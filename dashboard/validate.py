"""Form checking for the praise team's dashboard.

The people using this are volunteers doing it once a week between other jobs,
often on a phone. They should never have to understand what went wrong — a
mistake should be caught before it becomes a failed job, and the message should
say what to do next in the words they would use themselves.

So every check here returns plain language, points at the field that needs
fixing, and keeps what was already typed. Nothing says "invalid", "malformed"
or "error 400". The common real mistakes are caught by name:

  * a playlist link instead of a single song
  * a Spotify or Apple Music link, which cannot be downloaded from
  * a search results page pasted out of the address bar
  * the same song queued twice by two different people
  * the original recording left blank on an instrumental job
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

MAX_TITLE = 120
MAX_ARTIST = 120

#: Hosts yt-dlp handles well, which is what the worker downloads with.
SUPPORTED_HOSTS = (
    "youtube.com", "youtu.be", "music.youtube.com", "m.youtube.com",
    "vimeo.com", "soundcloud.com", "dailymotion.com", "archive.org",
    "drive.google.com", "dropbox.com",
)

#: Hosts people reach for that will not work, and what to say about each.
UNSUPPORTED_HOSTS = {
    "open.spotify.com": "Spotify links can't be downloaded from. "
                        "Search for the same song on YouTube and paste that link instead.",
    "spotify.com": "Spotify links can't be downloaded from. "
                   "Search for the same song on YouTube and paste that link instead.",
    "music.apple.com": "Apple Music links can't be downloaded from. "
                       "Search for the same song on YouTube and paste that link instead.",
    "apple.com": "Apple Music links can't be downloaded from. "
                 "Search for the same song on YouTube and paste that link instead.",
    "tidal.com": "Tidal links can't be downloaded from. Try a YouTube link instead.",
    "amazon.com": "Amazon Music links can't be downloaded from. "
                  "Try a YouTube link instead.",
    "facebook.com": "Facebook links usually don't work. Try a YouTube link instead.",
    "instagram.com": "Instagram links usually don't work. Try a YouTube link instead.",
}


@dataclass
class Errors:
    """Field name -> what to tell the person about it."""

    fields: dict = field(default_factory=dict)

    def add(self, name: str, message: str) -> None:
        self.fields.setdefault(name, message)

    def __bool__(self) -> bool:
        return bool(self.fields)

    def get(self, name: str) -> str:
        return self.fields.get(name, "")

    @property
    def first(self) -> str:
        return next(iter(self.fields.values()), "")


def tidy(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def check_link(raw: str, label: str, errors: Errors, name: str) -> str:
    """Validate one media link, returning it cleaned up."""
    link = (raw or "").strip()
    if not link:
        errors.add(name, f"{label} is needed before this can be made.")
        return ""

    # People paste with the scheme missing surprisingly often.
    if not link.lower().startswith(("http://", "https://")):
        link = "https://" + link

    try:
        parsed = urlparse(link)
    except ValueError:
        errors.add(name, "That doesn't look like a link. Copy it again from "
                         "your browser's address bar.")
        return link

    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        errors.add(name, "That doesn't look like a link. Copy it again from "
                         "your browser's address bar.")
        return link

    for bad, message in UNSUPPORTED_HOSTS.items():
        if host == bad or host.endswith("." + bad):
            errors.add(name, message)
            return link

    if not any(host == good or host.endswith("." + good)
               for good in SUPPORTED_HOSTS):
        errors.add(name, f"We can't download from {host}. A YouTube link is "
                         f"the safest thing to paste.")
        return link

    # A search results page rather than a song.
    if "youtube" in host and parsed.path.rstrip("/") in ("/results", "/feed"):
        errors.add(name, "That's a search results page, not a song. "
                         "Open the song itself, then copy the link.")
        return link

    # A whole playlist rather than one song.
    query = parse_qs(parsed.query or "")
    if "youtube" in host:
        has_video = bool(query.get("v")) or parsed.path.startswith("/shorts/")
        if parsed.path.rstrip("/") == "/playlist" or (query.get("list") and not has_video):
            errors.add(name, "That's a whole playlist. Open the one song you "
                             "want and copy that link instead.")
            return link
        if not has_video and host != "youtu.be":
            errors.add(name, "That link doesn't point at a particular song. "
                             "Open the song and copy the link from there.")
            return link
    if host == "youtu.be" and len(parsed.path.strip("/")) < 5:
        errors.add(name, "That YouTube link looks incomplete. Copy it again.")

    return link


def normalise_link(link: str) -> str:
    """A comparable form of a link, for spotting the same song twice.

    Two people sending the same song with different tracking parameters, or one
    from the phone app and one from a laptop, should still be recognised as a
    duplicate rather than rendered twice.
    """
    try:
        parsed = urlparse(link)
    except ValueError:
        return link.strip().lower()
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    query = parse_qs(parsed.query or "")
    if "youtu.be" in host:
        return f"yt:{parsed.path.strip('/')}"
    if "youtube" in host:
        if parsed.path.startswith("/shorts/"):
            return f"yt:{parsed.path.split('/shorts/', 1)[1].strip('/')}"
        video = query.get("v", [""])[0]
        if video:
            return f"yt:{video}"
    return f"{host}{parsed.path.rstrip('/')}".lower()


ACTIVE_STAGES = ("queued", "fetching", "extracting", "review",
                 "approved", "rendering")


def check_new_song(form, existing: list, transpose_choices,
                   uploaded_name: str = "") -> tuple:
    """Validate the 'add a song' form.

    Returns (cleaned values, Errors). `existing` is the current job list, used
    only to warn about queueing the same song twice.
    """
    errors = Errors()

    title = tidy(form.get("title"), MAX_TITLE)
    if not title:
        errors.add("title", "Give the song a name so everyone can find it later.")
    elif len(title) < 2:
        errors.add("title", "That name is too short to be useful.")

    source = form.get("source", "lyric_video")
    if source not in ("lyric_video", "instrumental"):
        source = "lyric_video"

    # A file and a link are alternatives. Uploading is the reliable route:
    # YouTube refuses a lot of music to anything that isn't a browser, and the
    # team generally already has the file they were going to play anyway.
    raw_link = (form.get("source_ref") or "").strip()
    if uploaded_name:
        link = f"upload:{uploaded_name}"
    elif raw_link:
        link = check_link(raw_link, "A link to the song", errors, "source_ref")
    else:
        link = ""
        errors.add("source_ref",
                   "Either paste a link or choose a video file to upload.")

    original = ""
    if source == "instrumental":
        original = check_link(
            form.get("original_ref"),
            "A link to the original recording (the one with singing)",
            errors, "original_ref")
        if (original and link
                and normalise_link(original) == normalise_link(link)):
            errors.add("original_ref",
                       "That's the same link as the instrumental. The second "
                       "one needs to be the version with singing on it.")

    theme = form.get("theme", "")
    try:
        transpose = int(form.get("transpose") or 0)
    except (TypeError, ValueError):
        transpose = 0
    if transpose not in transpose_choices:
        transpose = 0

    if link and not link.startswith("upload:") and not errors.get("source_ref"):
        key = normalise_link(link)
        for job in existing:
            if job.get("stage") not in ACTIVE_STAGES:
                continue
            if normalise_link(job.get("source_ref", "")) == key:
                errors.add("source_ref",
                           f"This song is already in the queue as "
                           f"“{job.get('title') or 'Untitled'}”. "
                           f"Open it from the queue instead of adding it again.")
                break

    return {
        "title": title,
        "artist": tidy(form.get("artist"), MAX_ARTIST),
        "source": source,
        "source_ref": link,
        "original_ref": original,
        "theme": theme,
        "transpose": transpose,
    }, errors


#: A .lyr line: [mm:ss.cc -> mm:ss.cc] words
_LINE = re.compile(r"^\[\s*\d+:\d{1,2}(?:\.\d+)?\s*->\s*\d+:\d{1,2}(?:\.\d+)?\s*\]")


def check_lyrics(text: str) -> tuple:
    """Check edited lyrics still parse, and count what survived.

    People edit this in a plain text box, so the realistic damage is a deleted
    bracket or a timestamp typed over. Catching it here means they get told
    immediately rather than the render failing minutes later on another machine.
    """
    errors = Errors()
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines, broken = 0, []

    for number, row in enumerate(raw.split("\n"), start=1):
        if not row.strip() or row.strip().startswith("#"):
            continue
        if _LINE.match(row):
            lines += 1
        elif row[:1] in (" ", "\t"):
            continue          # an indented continuation row, which is fine
        else:
            broken.append(number)

    if not lines:
        errors.add("lyrics", "There are no lyric lines here. Each one needs to "
                             "start with its timing, like [00:12.40 -> 00:16.80].")
    elif broken:
        shown = ", ".join(str(n) for n in broken[:4])
        more = f" and {len(broken) - 4} more" if len(broken) > 4 else ""
        errors.add("lyrics",
                   f"Line {shown}{more} doesn't start with a timing. Every line "
                   f"needs one, like [00:12.40 -> 00:16.80] — or indent it to "
                   f"make it part of the line above.")

    return raw, lines, errors
