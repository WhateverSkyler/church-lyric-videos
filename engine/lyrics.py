"""The timed-lyric data model, and the formats it round-trips through.

Two representations, deliberately:

  JSON  canonical. What the queue stores and the renderer consumes.
  .lyr  a plain-text format built for the human proofread step, because every
        automatic path into this program (OCR, forced alignment) gets a word
        wrong now and then and somebody has to fix it quickly.

A .lyr file looks like:

    # Song Title
    [00:12.40 -> 00:16.80] first line as it should appear
    [00:16.90 -> 00:21.10] second line
                           continued on a second display row

Indented continuation rows become a literal newline in the rendered card, so
the person proofreading controls line breaks without touching any timings.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: [mm:ss.cc -> mm:ss.cc] text
_LINE_RE = re.compile(
    r"^\[\s*(?P<s_m>\d+):(?P<s_s>\d{1,2}(?:\.\d+)?)\s*->\s*"
    r"(?P<e_m>\d+):(?P<e_s>\d{1,2}(?:\.\d+)?)\s*\]\s?(?P<text>.*)$"
)


def _fmt_ts(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:05.2f}"


@dataclass
class LyricLine:
    """One displayed card: the text, and when it is on screen."""

    text: str
    start: float
    end: float
    #: Optional structural label ("verse", "chorus"), kept for future themes
    #: that want to style a chorus differently. Nothing consumes it yet.
    section: str = ""
    #: Set when the text came from OCR or alignment and looked uncertain, so
    #: the dashboard can highlight it for the person proofreading.
    suspect: bool = False

    def __post_init__(self):
        self.start = float(self.start)
        self.end = float(self.end)
        if self.end < self.start:
            self.start, self.end = self.end, self.start

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class LyricTrack:
    """All the lines for one song, plus what we know about the song."""

    lines: list = field(default_factory=list)
    title: str = ""
    artist: str = ""
    source: str = ""

    # ---- ordering / hygiene ------------------------------------------

    def sorted(self) -> "LyricTrack":
        return LyricTrack(sorted(self.lines, key=lambda l: (l.start, l.end)),
                          self.title, self.artist, self.source)

    def tidy(self, min_duration: float = 0.55, gap: float = 0.06) -> "LyricTrack":
        """Drop empties, merge duplicates, and stop cards from overlapping.

        OCR in particular produces runs of the same text across consecutive
        sampled frames; those collapse into one card here.
        """
        out = []
        for line in sorted(self.lines, key=lambda l: (l.start, l.end)):
            text = " ".join(line.text.split("\n"))
            text = re.sub(r"[ \t]+", " ", text).strip()
            if not text:
                continue
            line.text = line.text.strip()
            if out and out[-1].text.strip().lower() == line.text.strip().lower() \
                    and line.start - out[-1].end < 1.2:
                # Same words again a moment later: extend rather than re-show.
                out[-1].end = max(out[-1].end, line.end)
                out[-1].suspect = out[-1].suspect or line.suspect
                continue
            if out and line.start < out[-1].end:
                # Overlap: give the earlier card until just before this one.
                out[-1].end = max(out[-1].start + min_duration, line.start - gap)
            out.append(line)

        return LyricTrack([l for l in out if l.duration >= min_duration * 0.5],
                          self.title, self.artist, self.source)

    def clamp(self, duration: float) -> "LyricTrack":
        """Trim any card that runs past the end of the audio."""
        out = []
        for line in self.lines:
            if line.start >= duration:
                continue
            line.end = min(line.end, duration)
            out.append(line)
        return LyricTrack(out, self.title, self.artist, self.source)

    def shift(self, seconds: float) -> "LyricTrack":
        """Nudge every card. The fix when a whole track sits early or late."""
        for line in self.lines:
            line.start = max(0.0, line.start + seconds)
            line.end = max(0.0, line.end + seconds)
        return self

    # ---- serialisation ------------------------------------------------

    def to_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "title": self.title,
            "artist": self.artist,
            "source": self.source,
            "lines": [asdict(l) for l in self.lines],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    @classmethod
    def from_json(cls, path: Path) -> "LyricTrack":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            lines=[LyricLine(**l) for l in data.get("lines", [])],
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            source=data.get("source", ""),
        )

    def to_lyr(self, path: Path) -> Path:
        """Write the human-editable proofreading format."""
        path.parent.mkdir(parents=True, exist_ok=True)
        out = []
        if self.title:
            out.append(f"# {self.title}")
        if self.artist:
            out.append(f"# artist: {self.artist}")
        if self.source:
            out.append(f"# source: {self.source}")
        if out:
            out.append("")
        for line in sorted(self.lines, key=lambda l: l.start):
            stamp = f"[{_fmt_ts(line.start)} -> {_fmt_ts(line.end)}] "
            rows = line.text.split("\n")
            out.append(stamp + rows[0])
            for row in rows[1:]:
                out.append(" " * len(stamp) + row)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return path

    @classmethod
    def from_lyr(cls, path: Path) -> "LyricTrack":
        track = cls(source=str(path))
        current = None
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith("#"):
                body = raw.strip().lstrip("#").strip()
                if body.lower().startswith("artist:"):
                    track.artist = body.split(":", 1)[1].strip()
                elif body.lower().startswith("source:"):
                    track.source = body.split(":", 1)[1].strip()
                elif not track.title:
                    track.title = body
                continue
            match = _LINE_RE.match(raw)
            if match:
                start = int(match["s_m"]) * 60 + float(match["s_s"])
                end = int(match["e_m"]) * 60 + float(match["e_s"])
                current = LyricLine(match["text"].rstrip(), start, end)
                track.lines.append(current)
            elif raw.strip() and current is not None:
                # An indented continuation row -> an explicit display break.
                current.text += "\n" + raw.strip()
        return track

    @classmethod
    def load(cls, path: Path) -> "LyricTrack":
        path = Path(path)
        return cls.from_json(path) if path.suffix == ".json" else cls.from_lyr(path)

    def __len__(self) -> int:
        return len(self.lines)
