"""Reads lyrics and their timings straight off a source lyric video.

This is what makes Phase 1 work without anyone typing or timing anything: the
source video already contains the words *and* their exact timing, burned into
the pixels. Recover both and the original video can be thrown away.

Why it doesn't OCR every frame: a five-minute video is ~9,000 frames but only
carries perhaps 60 distinct lyric cards. So the video is swept cheaply for
*changes* first, consecutive identical frames collapse into segments, and only
one representative frame per segment is ever handed to the OCR engine. That
turns thousands of OCR calls into dozens.

Text isolation relies on a property that holds for essentially every lyric
video: the type is near-white and *desaturated*, while whatever is behind it —
sky, footage, gradient — carries colour. Thresholding on brightness AND low
saturation separates them where thresholding on brightness alone fails, which
matters most exactly where it's hardest, over a bright sky.
"""

from __future__ import annotations

import re
import shutil
import string
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import tools
from .lyrics import LyricLine, LyricTrack

#: Frames per second to sweep. Lyric cards last seconds, so 4/s locates a
#: boundary to within 250 ms — finer than anyone can perceive while singing.
SWEEP_FPS = 4.0

#: Brightness (0-255) and max saturation (0-1) for a pixel to count as type.
BRIGHT = 232
MAX_SAT = 0.34

#: A frame with fewer than this fraction of masked pixels is treated as
#: carrying no text at all.
MIN_INK = 0.0016
#: ...and more than this is a title card, logo wall or a blown-out frame.
MAX_INK = 0.20


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------


def text_mask(rgb: np.ndarray, bright: int = BRIGHT,
              max_sat: float = MAX_SAT) -> np.ndarray:
    """Binary mask of likely text pixels in an (h, w, 3) uint8 frame."""
    a = rgb.astype(np.float32)
    mx = a.max(2)
    mn = a.min(2)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    return ((mx > bright) & (sat < max_sat))


def ink(mask: np.ndarray) -> float:
    return float(mask.mean())


#: Fingerprint grid. This must be fine enough to resolve individual words:
#: at a coarse 12x48 grid, 720p text lands inside one or two 60px-tall rows,
#: so two completely different lines produce nearly identical signatures and
#: the segmenter merges the whole song. 54x96 puts several cells across every
#: word, which is what actually distinguishes one card from the next.
FP_ROWS = 54
FP_COLS = 96


def fingerprint(mask: np.ndarray, rows: int = FP_ROWS,
                cols: int = FP_COLS) -> np.ndarray:
    """A downsampled signature of where the ink sits, for frame comparison."""
    h, w = mask.shape
    ch, cw = max(1, h // rows), max(1, w // cols)
    trimmed = mask[: (h // ch) * ch, : (w // cw) * cw]
    if trimmed.size == 0:
        return np.zeros(1, dtype=np.float32)
    blocks = trimmed.reshape(trimmed.shape[0] // ch, ch,
                             trimmed.shape[1] // cw, cw)
    return blocks.mean(axis=(1, 3)).astype(np.float32).ravel()


def distance(a: np.ndarray, b: np.ndarray) -> float:
    """Scale-invariant difference between two fingerprints, 0 (same) to 1.

    Plain mean-absolute-difference does NOT work here and silently destroys
    the segmentation: text occupies only ~4% of a frame, so ~96% of cells are
    empty in both fingerprints and drag the mean to nearly zero. Measured on a
    real lyric video, consecutive-frame distances had a median of 0.0000 and a
    *maximum* of 0.048 — meaning any threshold loose enough to tolerate
    encoder noise was also loose enough to merge every card in the song.

    Normalising by the total ink present makes the metric independent of how
    much text is on screen, so a genuine card change lands near 1.0 and
    compression jitter stays near 0.
    """
    if a.shape != b.shape:
        return 1.0
    total = float(a.sum() + b.sum())
    if total <= 1e-6:
        return 0.0
    return float(np.abs(a - b).sum() / total)


def similar(a: np.ndarray, b: np.ndarray, tol: float = 0.30) -> bool:
    return distance(a, b) < tol


# --------------------------------------------------------------------------
# sweeping the video
# --------------------------------------------------------------------------


def probe_size(video: Path) -> tuple:
    out = subprocess.run(
        [tools.ffprobe(), "-v", "error",
         "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", str(video)],
        capture_output=True, text=True)
    try:
        w, h = out.stdout.strip().split("x")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        return (1280, 720)


def sweep(video: Path, fps: float = SWEEP_FPS, progress=None):
    """Yield (timestamp, mask) for frames across the whole video."""
    w, h = probe_size(video)
    frame_bytes = w * h * 3
    proc = subprocess.Popen(
        [tools.ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-i", str(video), "-vf", f"fps={fps}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=frame_bytes * 4)
    n = 0
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            yield n / fps, text_mask(frame)
            n += 1
            if progress and n % 40 == 0:
                progress("sweep", n, 0)
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@dataclass
class Segment:
    start: float
    end: float
    mask: np.ndarray
    frames: int = 1
    #: True once the start time has been refined to native frame precision.
    refined: bool = False


# --------------------------------------------------------------------------
# boundary refinement
# --------------------------------------------------------------------------


def native_fps(video: Path) -> float:
    out = subprocess.run(
        [tools.ffprobe(), "-v", "error",
         "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True)
    try:
        num, den = out.stdout.strip().split("/")
        return float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return 30.0


def _window_frames(video: Path, start: float, duration: float, size: tuple):
    """Decode a short window at native rate. Yields (timestamp, mask)."""
    w, h = size
    frame_bytes = w * h * 3
    proc = subprocess.Popen(
        [tools.ffmpeg(), "-hide_banner", "-loglevel", "error",
         # -ss before -i seeks by keyframe (fast), then -ss after trims exactly.
         "-ss", f"{max(0.0, start - 2.0):.3f}", "-i", str(video),
         "-ss", f"{min(2.0, start):.3f}", "-t", f"{duration:.3f}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=frame_bytes * 4)
    fps = None
    try:
        n = 0
        while True:
            raw = proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
            yield n, text_mask(frame)
            n += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def refine_start(video: Path, coarse_start: float, target: np.ndarray,
                 size: tuple, fps: float, lookback: float = 0.40,
                 lookahead: float = 0.25) -> float:
    """Pin a card's start time to the frame it actually appears on.

    The coarse sweep runs at SWEEP_FPS to keep the OCR cost sane, which leaves
    each boundary uncertain by up to 1/SWEEP_FPS — 250 ms at 4/s. A quarter of
    a second is invisible when reading a transcript and very visible when a
    singer is watching for a cue, so every boundary is re-examined here at the
    video's native frame rate.

    Lyric videos usually FADE text in rather than cutting to it, so there is no
    single frame where the words switch on. The onset is therefore defined as
    the frame where the card reaches half its settled ink coverage, which is
    both stable against fade length and close to when a person would say the
    line became readable.
    """
    window_start = max(0.0, coarse_start - lookback)
    duration = lookback + lookahead
    target_ink = float(target.mean())
    if target_ink <= 0:
        return coarse_start

    samples = []
    for n, mask in _window_frames(video, window_start, duration, size):
        # Compare against the card we are looking for, not just any ink: a
        # dissolve between two cards has plenty of ink from the outgoing one.
        overlap = float((mask & target).sum())
        samples.append((n, overlap / max(1.0, float(target.sum()))))

    if not samples:
        return coarse_start

    settled = max(v for _, v in samples)
    if settled <= 0.05:
        return coarse_start

    threshold = settled * 0.5
    for n, value in samples:
        if value >= threshold:
            return round(window_start + n / fps, 3)
    return coarse_start


def refine_end(video: Path, coarse_end: float, target: np.ndarray,
               size: tuple, fps: float, lookback: float = 0.35,
               lookahead: float = 0.40) -> float:
    """Pin the frame a card falls below half its ink — the mirror of refine_start."""
    window_start = max(0.0, coarse_end - lookback)
    duration = lookback + lookahead
    if float(target.sum()) <= 0:
        return coarse_end

    samples = []
    for n, mask in _window_frames(video, window_start, duration, size):
        overlap = float((mask & target).sum())
        samples.append((n, overlap / max(1.0, float(target.sum()))))

    if not samples:
        return coarse_end
    settled = max(v for _, v in samples)
    if settled <= 0.05:
        return coarse_end

    threshold = settled * 0.5
    # Walk backwards to the last frame that was still readable.
    for n, value in reversed(samples):
        if value >= threshold:
            return round(window_start + n / fps, 3)
    return coarse_end


#: A measured gap smaller than this means the cards butt against each other,
#: and the outgoing card should simply run until the next one arrives rather
#: than blinking off for a few frames in between.
BUTT_GAP = 0.45


def refine_segments(video: Path, segments: list, progress=None) -> list:
    """Re-time every segment boundary at the video's native frame rate."""
    if not segments:
        return segments
    size = probe_size(video)
    fps = native_fps(video)

    for i, seg in enumerate(segments):
        seg.start = refine_start(video, seg.start, seg.mask, size, fps)
        seg.end = refine_end(video, seg.end, seg.mask, size, fps)
        seg.refined = True
        if progress:
            progress("refine", i + 1, len(segments))

    # Close small gaps so a card holds until its replacement actually appears.
    for a, b in zip(segments, segments[1:]):
        if 0 <= b.start - a.end <= BUTT_GAP:
            a.end = b.start
        if a.end > b.start:
            a.end = b.start
    return segments


def segment(video: Path, fps: float = SWEEP_FPS, min_hold: float = 0.55,
            progress=None) -> list:
    """Collapse the sweep into runs of frames showing the same thing."""
    segments = []
    current = None
    current_fp = None

    for t, mask in sweep(video, fps, progress):
        density = ink(mask)
        blank = density < MIN_INK or density > MAX_INK
        fp = None if blank else fingerprint(mask)

        if blank:
            if current is not None:
                current.end = t
                segments.append(current)
                current, current_fp = None, None
            continue

        if current is not None and similar(current_fp, fp):
            current.end = t
            current.frames += 1
            # Keep the densest mask as the representative: mid-fade frames are
            # partially transparent and OCR far worse than a fully-on one.
            if ink(mask) > ink(current.mask):
                current.mask = mask
            continue

        if current is not None:
            current.end = t
            segments.append(current)
        current = Segment(start=t, end=t + 1.0 / fps, mask=mask)
        current_fp = fp

    if current is not None:
        segments.append(current)

    return [s for s in segments if s.end - s.start >= min_hold]


# --------------------------------------------------------------------------
# recognition
# --------------------------------------------------------------------------


class TesseractBackend:
    """Cross-platform default. Good enough on clean, isolated masks."""

    name = "tesseract"

    def __init__(self, psm: int = 6, lang: str = "eng"):
        self.psm = psm
        self.lang = lang
        # Resolved through tools rather than PATH: a SYSTEM scheduled task
        # does not inherit a user's PATH, so a perfectly good installation is
        # invisible to shutil.which() there.
        self.exe = tools.tesseract()
        if not self.exe:
            raise RuntimeError(
                "tesseract not found. Install it (winget install "
                "UB-Mannheim.TesseractOCR on Windows, brew install tesseract "
                "on macOS), or point HOPEWELL_TESSERACT at it."
            )

    def read(self, mask: np.ndarray, workdir: Path) -> str:
        from PIL import Image

        # Tesseract wants dark text on light ground.
        img = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), "L")
        path = workdir / "ocr.png"
        img.save(path)
        proc = subprocess.run(
            [self.exe, str(path), "-", "--psm", str(self.psm), "-l", self.lang],
            capture_output=True, text=True, errors="replace")
        return proc.stdout if proc.returncode == 0 else ""


class EasyOCRBackend:
    """GPU backend for the church PC. Far more accurate on stylised type.

    Loaded lazily so the module imports fine on machines without torch.
    """

    name = "easyocr"

    def __init__(self, gpu: bool = True):
        import easyocr  # noqa: F401

        self._reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)

    def read(self, mask: np.ndarray, workdir: Path) -> str:
        """Detected fragments, reassembled into reading order.

        The detector returns boxes in no particular order, so they have to be
        put back into rows and then left-to-right WITHIN each row. Sorting by
        vertical position alone - which an earlier version did - leaves words
        joined in detection order, and a line comes out with its words
        shuffled. It looks like plausible English, which is what makes it
        dangerous: it reads as a real line and would go on screen unnoticed.

        Rows are grouped by overlap against the median glyph height rather
        than a fixed pixel gap, so it holds at any type size.
        """
        img = np.where(mask, 0, 255).astype(np.uint8)
        found = self._reader.readtext(img, detail=1, paragraph=False)
        if not found:
            return ""

        items = []
        for box, text, conf in found:
            if not (text or "").strip():
                continue
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            items.append({
                "text": text.strip(),
                "top": min(ys),
                "mid": (min(ys) + max(ys)) / 2.0,
                "height": max(ys) - min(ys),
                "left": min(xs),
                "conf": conf,
            })
        if not items:
            return ""

        heights = sorted(i["height"] for i in items)
        typical = heights[len(heights) // 2] or 1.0
        # Two fragments belong to the same row when their centres sit within
        # roughly half a line of each other.
        tolerance = max(8.0, typical * 0.6)

        rows: list = []
        for item in sorted(items, key=lambda i: i["mid"]):
            if rows and abs(item["mid"] - rows[-1]["mid"]) <= tolerance:
                rows[-1]["items"].append(item)
                # Track the running centre so a gently drifting row still binds.
                rows[-1]["mid"] = sum(x["mid"] for x in rows[-1]["items"]) / len(rows[-1]["items"])
            else:
                rows.append({"mid": item["mid"], "items": [item]})

        lines = []
        for row in rows:
            ordered = sorted(row["items"], key=lambda i: i["left"])
            line = " ".join(i["text"] for i in ordered).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)


def make_backend(prefer: str = "auto"):
    """Pick the best available OCR engine.

    When neither works, the error names BOTH causes. Reporting only the
    Tesseract failure — which is what a bare fallback does — points at the
    wrong thing entirely on a machine where EasyOCR is the intended engine and
    something about its install is broken.
    """
    easy_error = None
    if prefer in ("auto", "easyocr"):
        try:
            return EasyOCRBackend()
        except Exception as exc:
            if prefer == "easyocr":
                raise
            easy_error = exc

    try:
        return TesseractBackend()
    except RuntimeError as tess_error:
        if easy_error is None:
            raise
        raise RuntimeError(
            "No OCR engine is usable, so the words cannot be read off a "
            f"video.\n  EasyOCR (preferred): {easy_error}\n"
            f"  Tesseract (fallback): {tess_error}"
        ) from easy_error


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

#: Characters OCR routinely invents in place of a capital I or l.
_I_CONFUSIONS = ("|", "!", "¡", "1")

#: Characters this OCR puts where a letter belongs on lyric-video type. Each
#: one is a character that cannot occur in that position in English, so a word
#: that was read correctly passes through these untouched.
_GLYPH_REPAIRS = (
    ("€", "G"),          # "goodness of €od"
    ("£", "E"),
    ("$", "S"),
    ("§", "S"),
    ("©", "C"),
    ("®", "R"),
    ("¥", "Y"),
    ("¤", "O"),
)

#: Everything that may legitimately appear in a lyric line. A character
#: outside this set is a recognition artifact, never a word.
#:
#: The repairs above name the substitutions actually seen, but they can only
#: ever cover what has already gone wrong once. This set is the guarantee
#: underneath them: whatever the engine invents next, a symbol does not reach
#: a sanctuary screen. A misread word is a misread word - a euro sign in the
#: middle of "God" is a different kind of wrong, and the one nobody forgives.
_SAFE_CHARS = frozenset(string.ascii_letters + " '\u2019-,.!?;:()\"")

#: A double quote between two letters is a mangled apostrophe: I"ve -> I've.
_MANGLED_APOSTROPHE = re.compile(r'(?<=[A-Za-z])"(?=[A-Za-z])')

#: No English contraction begins with F. The letter OCR loses here is a
#: capital I, which on this type it reads as F: F've -> I've.
#: Case-insensitive because repairs run before a SHOUTED line is normalised,
#: so the raw text may still read F'VE.
_F_CONTRACTION = re.compile(r"^F(?='(?:ve|m|ll|d|re)$)", re.IGNORECASE)

#: A leading slash standing in for a lowercase i: "/s" -> "is".
_LEADING_SLASH = re.compile(r"^/(?=[A-Za-z])")

#: Words common enough after the pronoun that an "I" glued to the next word
#: can be split with confidence. Deliberately short: "Israel", "Isaiah" and
#: "Immanuel" are all worship vocabulary, and splitting on any lowercase run
#: would break every one of them.
_I_GLUED_FOLLOWERS = {
    "love", "will", "am", "have", "know", "need", "see", "sing", "give",
    "praise", "worship", "lift", "come", "want", "believe", "trust", "call",
    "feel", "surrender", "receive", "sought", "found", "run", "stand",
}

#: An "I" glued to the front of a lowercase word: "Ilove" -> "I love".
_I_GLUED = re.compile(r"^I([A-Za-z]{2,})$")

#: Words that stay capitalised when a SHOUTED source line is normalised back
#: to sentence case. Reverent capitalisation is the convention in printed
#: worship lyrics, and getting it wrong is the kind of thing a congregation
#: notices immediately.
_ALWAYS_CAPITAL = {
    "god", "lord", "jesus", "christ", "father", "saviour", "savior",
    "spirit", "holy", "king", "almighty", "messiah", "emmanuel",
    "immanuel", "yahweh", "abba", "redeemer", "lamb", "shepherd",
    "i", "i'm", "i've", "i'll", "i'd",
}

#: Pronouns referring to God are conventionally capitalised mid-line too, but
#: only when the surrounding line is about Him — too risky to guess, so these
#: are left alone and the person reviewing can adjust.
_SHOUT_RATIO = 0.75


def normalise_case(text: str) -> str:
    """Convert a SHOUTED source line back to sentence case.

    Lyric videos overwhelmingly burn their text in capitals, and OCR faithfully
    reproduces that. Carrying it through would mean every Hopewell video
    inherits the styling of whichever YouTube video it came from — the exact
    inconsistency this tool exists to remove. Case is a *theme* decision, so
    the text is normalised here and each theme re-applies whatever it wants.

    Mixed-case input is left completely alone; it already carries real
    capitalisation worth preserving.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    if sum(c.isupper() for c in letters) / len(letters) < _SHOUT_RATIO:
        return text

    out_rows = []
    for row in text.split("\n"):
        words = row.split()
        rebuilt = []
        for i, word in enumerate(words):
            bare = word.lower()
            stripped = bare.strip(".,!?;:\"'()")
            if stripped in _ALWAYS_CAPITAL or i == 0:
                # Capitalise the first letter, leaving any leading quote alone.
                lowered = list(bare)
                for j, ch in enumerate(lowered):
                    if ch.isalpha():
                        lowered[j] = ch.upper()
                        break
                rebuilt.append("".join(lowered))
            else:
                rebuilt.append(bare)
        out_rows.append(" ".join(rebuilt))
    return "\n".join(out_rows)


def repair_word(word: str) -> str:
    """Undo the character-level confusions this OCR makes on lyric type.

    Every rule is keyed on a character that cannot legally sit where it was
    found, so a word that was read correctly cannot be damaged by any of
    them. That is the whole basis for doing this without a dictionary: the
    engine also confuses g for c ("throuch", "runninc", "cood"), which looks
    like the same class of fault but is not - c is a perfectly legal letter
    in those positions, and repairing it needs a vocabulary to know that
    "cood" is not a word while "come" is.
    """
    for wrong, right in _GLYPH_REPAIRS:
        word = word.replace(wrong, right)
    word = _MANGLED_APOSTROPHE.sub("'", word)
    word = _F_CONTRACTION.sub("I", word)
    word = _LEADING_SLASH.sub("i", word)
    glued = _I_GLUED.match(word)
    if glued and glued.group(1).lower() in _I_GLUED_FOLLOWERS:
        word = f"I {glued.group(1)}"
    # Whatever survived the named repairs and still is not a letter never
    # belonged to the song. Dropping it leaves a misspelling, which is the
    # kind of wrong a congregation reads past.
    return "".join(c for c in word if c in _SAFE_CHARS)


def clean(raw: str) -> str:
    """Tidy one OCR result into something worth showing a human."""
    lines = []
    for row in raw.splitlines():
        row = " ".join(row.split())
        if not row:
            continue
        # A standalone bar/bang is nearly always the pronoun "I".
        words = []
        for word in row.split():
            if word in _I_CONFUSIONS:
                word = "I"
            words.append(repair_word(word))
        row = " ".join(words)
        # Drop rows that are mostly punctuation — usually a stray logo edge.
        letters = sum(c.isalpha() for c in row)
        if letters < 2 or letters < len(row) * 0.45:
            continue
        lines.append(row)
    return normalise_case("\n".join(lines))


#: Words that mark a card as the source video's own branding rather than a
#: lyric. These are what an uploader puts on the title screen, and OCR reads
#: them just as happily as it reads the song.
_TITLE_CARD_WORDS = {
    "instrumental", "karaoke", "cover", "lyrics", "lyric", "backing",
    "track", "playback", "accompaniment", "subscribe", "channel",
    "official", "audio", "video", "hd", "remastered", "minus", "one",
    "performance", "demo", "preview", "copyright", "records", "music",
    "productions", "ministries", "www", "com", "http", "https",
}

#: How long into a video a card may still be branding rather than singing.
TITLE_CARD_WINDOW = 30.0


def looks_like_title_card(text: str) -> bool:
    """Whether a card is the source's own title screen, not a lyric.

    Left in, it becomes the first thing on screen: the previous render opened
    on a mangled rendition of the uploader's branding, which is precisely the
    borrowed styling this whole program exists to get rid of.
    """
    words = [w.strip(".,!?;:\"'()-").lower() for w in text.split()]
    words = [w for w in words if w]
    if not words:
        return True

    hits = sum(1 for w in words if w in _TITLE_CARD_WORDS)
    if hits >= 2:
        return True
    if hits and len(words) <= 4:
        return True

    # OCR makes a mess of the script and logo type these cards favour, so a
    # card that is mostly unreadable this early is branding rather than words
    # anybody is expected to sing.
    odd = sum(1 for w in words if sum(c.isalpha() for c in w) < len(w) * 0.7)
    return odd >= max(2, len(words) * 0.4)


def looks_suspect(text: str) -> bool:
    """Flag text the person proofreading should look at first."""
    if not text:
        return True
    words = text.split()
    if not words:
        return True
    odd = sum(1 for w in words if sum(c.isalpha() for c in w) < len(w) * 0.6)
    return odd > len(words) * 0.25 or any(len(w) > 18 for w in words)


# --------------------------------------------------------------------------
# the public entry point
# --------------------------------------------------------------------------


def extract(video: Path, fps: float = SWEEP_FPS, backend=None,
            skip_head: float = 0.0, refine: bool = True,
            progress=None) -> LyricTrack:
    """Recover a timed LyricTrack from a burned-in lyric video.

    Args:
        skip_head: seconds to ignore at the start, for title cards. Segments
                   there are usually artist/title art, not lyrics.
        refine:    re-time every boundary at the video's native frame rate.
                   Leave this on — without it, cues carry up to 1/SWEEP_FPS of
                   error, which a singer watching for an entrance will feel.
    """
    backend = backend or make_backend()
    segments = segment(video, fps, progress=progress)
    if refine:
        segments = refine_segments(video, segments, progress=progress)

    track = LyricTrack(source=str(video))
    with tempfile.TemporaryDirectory(prefix="hopewell-ocr-") as tmp:
        workdir = Path(tmp)
        for i, seg in enumerate(segments):
            if seg.end <= skip_head:
                continue
            text = clean(backend.read(seg.mask, workdir))
            if not text:
                continue
            if seg.start < TITLE_CARD_WINDOW and looks_like_title_card(text):
                continue
            track.lines.append(LyricLine(
                text=text,
                start=max(0.0, seg.start),
                end=seg.end,
                suspect=looks_suspect(text),
            ))
            if progress:
                progress("ocr", i + 1, len(segments))

    return track.tidy()
