"""Phase 2: timing lyrics against an instrumental that has no vocals to hear.

You cannot transcribe an instrumental — there is nothing being sung. So the
timings are borrowed from a recording that *does* have vocals and then warped
onto the instrumental's own timeline:

    1. take the original studio recording of the song
    2. demucs splits the vocal off from the backing
    3. Whisper transcribes that isolated vocal with word-level timestamps
    4. chroma features + DTW map original-time onto instrumental-time
    5. every timestamp is carried across through that mapping

Step 4 is what makes this survive real life. A backing track is rarely the
exact same master minus vocals — it may be a different take, a different key
of the same arrangement, or simply slower. Chroma is the right feature for it:
it describes harmony as twelve pitch classes and largely ignores timbre, so
removing a vocal barely perturbs it while the chord movement that defines
where you are in the song stays intact.

When no original exists, `tap.py` covers the same ground manually.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import tools
from .lyrics import LyricLine, LyricTrack

SAMPLE_RATE = 16000
#: Feature frames per second for the alignment. 20/s locates a beat closely
#: enough for lyrics while keeping the DTW cost matrix tractable.
FEATURE_FPS = 20


# --------------------------------------------------------------------------
# audio loading
# --------------------------------------------------------------------------


def load_mono(path: Path, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any media file to a mono float32 waveform."""
    proc = subprocess.run(
        [tools.ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"],
        capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"could not decode {path}: "
                           f"{proc.stderr.decode('utf-8', 'replace')[:300]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


# --------------------------------------------------------------------------
# chroma features
# --------------------------------------------------------------------------


def _chroma_filterbank(n_fft: int, sr: int) -> np.ndarray:
    """(12, n_bins) matrix folding FFT bins into pitch classes."""
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    bank = np.zeros((12, len(freqs)), dtype=np.float32)
    # Ignore sub-bass and anything above the top of the musical range; both
    # are dominated by energy that says nothing about harmony.
    usable = (freqs > 55.0) & (freqs < 2200.0)
    # MIDI note number, then pitch class.
    midi = np.zeros_like(freqs)
    midi[usable] = 69 + 12 * np.log2(freqs[usable] / 440.0)
    classes = np.round(midi).astype(int) % 12
    for i in np.nonzero(usable)[0]:
        bank[classes[i], i] = 1.0
    return bank


def chroma(audio: np.ndarray, sr: int = SAMPLE_RATE,
           fps: int = FEATURE_FPS) -> np.ndarray:
    """(frames, 12) L2-normalised chroma. Robust to a missing vocal."""
    n_fft = 4096
    hop = max(1, int(sr / fps))
    if len(audio) < n_fft:
        audio = np.pad(audio, (0, n_fft - len(audio)))

    n_frames = 1 + (len(audio) - n_fft) // hop
    if n_frames <= 0:
        return np.zeros((1, 12), dtype=np.float32)

    window = np.hanning(n_fft).astype(np.float32)
    bank = _chroma_filterbank(n_fft, sr)

    out = np.empty((n_frames, 12), dtype=np.float32)
    # Chunked so a five-minute track doesn't materialise one huge STFT.
    step = 512
    for start in range(0, n_frames, step):
        stop = min(n_frames, start + step)
        idx = (np.arange(start, stop)[:, None] * hop
               + np.arange(n_fft)[None, :])
        frames = audio[idx] * window
        mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
        out[start:stop] = mag @ bank.T

    # Log-compress, then normalise each frame so loudness differences between
    # a master and a backing track don't dominate the distance.
    out = np.log1p(out)
    norm = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.maximum(norm, 1e-8)


# --------------------------------------------------------------------------
# dynamic time warping
# --------------------------------------------------------------------------


@dataclass
class Warp:
    """Maps a timestamp on the source timeline to the target timeline."""

    source_times: np.ndarray
    target_times: np.ndarray

    def __call__(self, t: float) -> float:
        return float(np.interp(t, self.source_times, self.target_times))

    def confidence(self) -> float:
        """0-1. Low means the two recordings drifted apart or don't match.

        Measured as how monotonic and how close to linear the mapping is; a
        good match between the same arrangement is nearly a straight line.
        """
        if len(self.source_times) < 3:
            return 0.0
        slope = np.gradient(self.target_times, self.source_times)
        finite = slope[np.isfinite(slope)]
        if not len(finite):
            return 0.0
        # Penalise both reversals and wild tempo swings.
        sane = float(((finite > 0.4) & (finite < 2.5)).mean())
        steadiness = float(1.0 / (1.0 + np.std(np.clip(finite, 0, 3))))
        return max(0.0, min(1.0, 0.5 * sane + 0.5 * steadiness))


def dtw(a: np.ndarray, b: np.ndarray, band: float = 0.25) -> Warp:
    """Align feature sequences `a` and `b` with a Sakoe-Chiba banded DTW.

    The band caps the cost matrix at a fraction of the full N*M and encodes a
    real assumption: two recordings of the same song never drift more than a
    quarter of the song apart. Without it a five-minute pair would be a
    36-million-cell DP.
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return Warp(np.array([0.0]), np.array([0.0]))

    radius = max(16, int(max(n, m) * band))
    inf = np.float32(np.inf)

    # cost[i, j] = 1 - cosine similarity, since chroma rows are unit vectors.
    cost = np.full((n, m), inf, dtype=np.float32)
    for i in range(n):
        lo = max(0, int(i * m / n) - radius)
        hi = min(m, int(i * m / n) + radius + 1)
        cost[i, lo:hi] = 1.0 - (a[i] @ b[lo:hi].T)

    acc = np.full((n, m), inf, dtype=np.float32)
    ptr = np.zeros((n, m), dtype=np.uint8)
    acc[0, 0] = cost[0, 0]
    for j in range(1, m):
        if np.isfinite(cost[0, j]):
            acc[0, j] = acc[0, j - 1] + cost[0, j]
            ptr[0, j] = 2
    for i in range(1, n):
        lo = max(0, int(i * m / n) - radius)
        hi = min(m, int(i * m / n) + radius + 1)
        if lo == 0:
            acc[i, 0] = acc[i - 1, 0] + cost[i, 0]
            ptr[i, 0] = 1
            lo = 1
        prev = acc[i - 1]
        row = acc[i]
        for j in range(lo, hi):
            diag, up, left = prev[j - 1], prev[j], row[j - 1]
            best, code = diag, 0
            if up < best:
                best, code = up, 1
            if left < best:
                best, code = left, 2
            if np.isfinite(best):
                row[j] = best + cost[i, j]
                ptr[i, j] = code

    # Backtrace from the corner.
    i, j = n - 1, m - 1
    pairs = []
    guard = 0
    while (i > 0 or j > 0) and guard < (n + m) * 2:
        pairs.append((i, j))
        code = ptr[i, j]
        if code == 0 and i > 0 and j > 0:
            i, j = i - 1, j - 1
        elif code == 1 and i > 0:
            i -= 1
        elif j > 0:
            j -= 1
        else:
            break
        guard += 1
    pairs.append((0, 0))
    pairs.reverse()

    src = np.array([p[0] for p in pairs], dtype=np.float64) / FEATURE_FPS
    tgt = np.array([p[1] for p in pairs], dtype=np.float64) / FEATURE_FPS
    # Collapse duplicate source times so np.interp gets a monotonic x.
    keep = np.concatenate(([True], np.diff(src) > 0))
    return Warp(src[keep], tgt[keep])


def build_warp(source_audio: Path, target_audio: Path) -> Warp:
    """DTW mapping from `source_audio`'s timeline onto `target_audio`'s."""
    a = chroma(load_mono(source_audio))
    b = chroma(load_mono(target_audio))
    return dtw(a, b)


# --------------------------------------------------------------------------
# stem separation and transcription
# --------------------------------------------------------------------------


def isolate_vocals(audio: Path, workdir: Path, model: str = "htdemucs") -> Path:
    """Split the vocal stem out with demucs. Returns the vocals wav."""
    workdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["python", "-m", "demucs", "--two-stems", "vocals",
         "-n", model, "-o", str(workdir), str(audio)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("demucs failed:\n"
                           + "\n".join(proc.stderr.strip().splitlines()[-15:]))
    matches = list(workdir.rglob("vocals.wav"))
    if not matches:
        raise RuntimeError(f"demucs produced no vocals stem under {workdir}")
    return matches[0]


def transcribe_words(vocals: Path, model_name: str = "medium",
                     language: str = "en") -> list:
    """Whisper transcription with word-level timestamps."""
    import whisper

    model = whisper.load_model(model_name)
    result = model.transcribe(str(vocals), word_timestamps=True,
                              language=language, verbose=False)
    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []) or []:
            token = (w.get("word") or "").strip()
            if token:
                words.append({"word": token,
                              "start": float(w["start"]),
                              "end": float(w["end"])})
    return words


def words_to_lines(words: list, max_words: int = 8, max_gap: float = 0.9,
                   max_duration: float = 6.0) -> LyricTrack:
    """Group word timings into displayable lyric cards."""
    track = LyricTrack()
    current = []
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            span = w["end"] - current[0]["start"]
            if gap > max_gap or len(current) >= max_words or span > max_duration:
                track.lines.append(LyricLine(
                    " ".join(x["word"] for x in current),
                    current[0]["start"], current[-1]["end"]))
                current = []
        current.append(w)
    if current:
        track.lines.append(LyricLine(
            " ".join(x["word"] for x in current),
            current[0]["start"], current[-1]["end"]))
    return track.tidy()


# --------------------------------------------------------------------------
# the public entry point
# --------------------------------------------------------------------------


@dataclass
class AlignResult:
    track: LyricTrack
    confidence: float
    words: int
    note: str = ""


def align_from_original(original: Path, instrumental: Path,
                        whisper_model: str = "medium",
                        workdir: Path | None = None,
                        progress=None) -> AlignResult:
    """Full Phase 2 path: original recording -> timings on the instrumental."""
    owned = workdir is None
    tmp = Path(tempfile.mkdtemp(prefix="hopewell-align-")) if owned else workdir

    try:
        if progress:
            progress("separate", 0, 3)
        vocals = isolate_vocals(original, tmp)

        if progress:
            progress("transcribe", 1, 3)
        words = transcribe_words(vocals, whisper_model)
        if not words:
            return AlignResult(LyricTrack(), 0.0, 0,
                               "Whisper found no words in the isolated vocal.")

        if progress:
            progress("warp", 2, 3)
        warp = build_warp(original, instrumental)
        confidence = warp.confidence()

        track = words_to_lines(words)
        for line in track.lines:
            line.start = warp(line.start)
            line.end = warp(line.end)
            # A weak warp means these timings need a human's eye.
            line.suspect = confidence < 0.55
        track = track.tidy()

        note = ""
        if confidence < 0.55:
            note = ("Low alignment confidence — the instrumental may be a "
                    "different arrangement. Check the timings, or use the "
                    "tap-timing tool instead.")
        return AlignResult(track, confidence, len(words), note)
    finally:
        if owned:
            shutil.rmtree(tmp, ignore_errors=True)


def retime(track: LyricTrack, source_audio: Path, target_audio: Path) -> LyricTrack:
    """Move an existing timed track from one recording's timeline to another.

    Useful when the church already has a timed version of a song and simply
    swaps to a different backing track.
    """
    warp = build_warp(source_audio, target_audio)
    for line in track.lines:
        line.start = warp(line.start)
        line.end = warp(line.end)
    return track.tidy()
