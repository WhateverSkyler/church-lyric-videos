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

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

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
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
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
        [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
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
        if not shutil.which("tesseract"):
            raise RuntimeError(
                "tesseract not found on PATH. brew install tesseract "
                "(macOS) or choco install tesseract (Windows)."
            )

    def read(self, mask: np.ndarray, workdir: Path) -> str:
        from PIL import Image

        # Tesseract wants dark text on light ground.
        img = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), "L")
        path = workdir / "ocr.png"
        img.save(path)
        proc = subprocess.run(
            ["tesseract", str(path), "-", "--psm", str(self.psm), "-l", self.lang],
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
        img = np.where(mask, 0, 255).astype(np.uint8)
        rows = self._reader.readtext(img, detail=1, paragraph=False)
        # Group by vertical position so multi-line cards keep their line breaks.
        rows = sorted(rows, key=lambda r: (min(p[1] for p in r[0])))
        lines, current, last_y = [], [], None
        for box, text, _conf in rows:
            y = min(p[1] for p in box)
            if last_y is not None and abs(y - last_y) > 18:
                lines.append(" ".join(current))
                current = []
            current.append(text)
            last_y = y
        if current:
            lines.append(" ".join(current))
        return "\n".join(lines)


def make_backend(prefer: str = "auto"):
    """Pick the best available OCR engine."""
    if prefer in ("auto", "easyocr"):
        try:
            return EasyOCRBackend()
        except Exception:
            if prefer == "easyocr":
                raise
    return TesseractBackend()


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

#: Characters OCR routinely invents in place of a capital I or l.
_I_CONFUSIONS = ("|", "!", "¡", "1")


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
            words.append(word)
        row = " ".join(words)
        # Drop rows that are mostly punctuation — usually a stray logo edge.
        letters = sum(c.isalpha() for c in row)
        if letters < 2 or letters < len(row) * 0.45:
            continue
        lines.append(row)
    return "\n".join(lines)


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
            skip_head: float = 0.0, progress=None) -> LyricTrack:
    """Recover a timed LyricTrack from a burned-in lyric video.

    Args:
        skip_head: seconds to ignore at the start, for title cards. Segments
                   there are usually artist/title art, not lyrics.
    """
    backend = backend or make_backend()
    segments = segment(video, fps, progress=progress)

    track = LyricTrack(source=str(video))
    with tempfile.TemporaryDirectory(prefix="hopewell-ocr-") as tmp:
        workdir = Path(tmp)
        for i, seg in enumerate(segments):
            if seg.end <= skip_head:
                continue
            text = clean(backend.read(seg.mask, workdir))
            if not text:
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
