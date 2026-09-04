# Hopewell Lyric Videos

Turns whatever the praise team finds into a lyric video that looks like it
belongs to Hopewell Baptist Church — same type, same colours, same mark, every
week.

The problem this solves: the team downloads instrumental-with-lyrics videos off
YouTube, and every one is styled differently. Sunday to Sunday the screens look
like they came from six different churches.

## How it works

```
  Praise team  ──▶  Dashboard on the VPS  ──▶  job queue
   (any phone,        one shared password         │
    any day)                                      │  the church PC polls
                                                  ▼  outbound over https
                                        Worker on the church PC
                                        RTX 3060 · OCR / Whisper / render
                                                  │
                                    ┌─────────────┴─────────────┐
                                    ▼                           ▼
                          local "Sunday" folder        uploaded for download
```

The queue lives on the VPS rather than the church computer so the team can add
songs any day of the week without the church machine being awake, and so
**nothing has to be opened on the church's network** — the worker only makes
outbound requests.

## The two ways in

**Phase 1 — you have a lyric video.** The usual case. The source already
contains the words *and* their exact timing, burned into the pixels, so both
are read straight off it. Nobody types or times anything.

**Phase 2 — you only have an instrumental.** There is nothing sung to
transcribe, so the timings are borrowed from the original recording: demucs
splits the vocal out, Whisper timestamps it word by word, and DTW maps that
onto the backing track's own timeline. Needs a link to the original as well.

Either way it is one submit and nothing else. The form asks three things —
song name, the video file, and the key — and the finished video appears
without coming back.

There is a real cost to that. OCR mis-reads a word occasionally, and with no
review step a wrong word reaches the screen. Three things carry that weight
instead of a gate:

- the misreads this engine actually makes are repaired — see "Reading the
  words" below
- **no symbol ever reaches a screen.** A misread word is forgivable; a euro
  sign in the middle of "God" is not, and one shipped once
- the finished video **plays on the job page**, so somebody can watch it
  before Sunday without downloading eighty megabytes to a phone. The words are
  editable there and the job re-renders

## The themes

Six looks, so a month of Sundays never repeats. All of them use the church's
own Merriweather/Lato and a palette sampled from the logo, and all carry the
mark somewhere.

| Key | Mood | Suits |
|---|---|---|
| `cinematic-warm` | warm | The default Sunday look |
| `navy-minimal` | clean | Maximum readability from the back row |
| `stained-glass` | reverent | Slower, more traditional songs |
| `sanctuary-dusk` | reflective | Hymns and invitations |
| `morning-light` | bright | Bright rooms where dark themes wash out |
| `hillside` | hopeful | Upbeat songs |

A look is chosen automatically, so consecutive weeks do not match.

Backgrounds are stock footage from a small library somebody has watched, laid
under a scrim so the motion reads without anyone following it. Every clip is
approved by hand before it can appear — a keyword search cannot be known in
advance, and unreviewed video has no business behind worship lyrics. Each
theme also carries a generated plate, used when no approved clip is
available. See "Backgrounds" below.

## Command line

```bash
./hopewell.py themes                       # list the theme pack
./hopewell.py preview --theme hillside     # one still, no render
./hopewell.py footage --per-mood 4         # refresh the footage library

# Phase 1
./hopewell.py prepare "<url or file>" --title "Song Name"
#   ...read the .lyr file it writes, fix anything misread...
./hopewell.py render work/<id>/lyrics.lyr work/<id>/audio.m4a --theme sanctuary-dusk

# Phase 2
./hopewell.py prepare "<instrumental>" --original "<original>" \
    --source instrumental --title "Song Name"
```

## Layout

```
engine/
  brand.py        palette, fonts and logo — sampled from the church's own mark
  themes/         the six looks; pure data plus two plate-builder functions
  textcard.py     one lyric line as a flat image (used for stills/previews)
  typeset.py      the same line as individually animatable word sprites
  textanim.py     how type enters, sits and leaves
  anim.py         easing, transforms, sprite compositing
  splash.py       the branded open and close, and the logo's travel between
  background.py   procedural backdrops, for when no footage is available
  footage.py      Pexels sourcing, grading, looping, approvals and rejections
  ocr.py          Phase 1 — lyrics and timing off a source video
  data/           lyric_words.txt — vocabulary the c/g repair is checked against
  align.py        Phase 2 — demucs + Whisper + chroma DTW
  transpose.py    key changes that provably do not move the timing
  compositor.py   the per-frame renderer
  render.py       encoder selection, logo placement, stills
  lyrics.py       the data model and the .lyr proofreading format
  pipeline.py     prepare() / render(), the two halves of a job
  tools.py        finds ffmpeg/yt-dlp without depending on PATH
dashboard/        Flask queue + web UI (runs on the VPS)
  validate.py     catches the mistakes people actually make, by name
worker/           the polling render worker (runs on the church PC)
  guard.py        keeps rendering away from the livestream
assets/footage/
  prepared/       the clips that actually go on screen (tracked, not ignored)
  catalog.json    every clip, its mood, and whether a person approved it
  rejected.json   clips turned down for good; no future fetch may re-add them
deploy/           deployment scripts — the DASHBOARD runs a deployed copy,
                  so a git push alone does not change the live site
scripts/
  verify_timing.py  proves cues match the source, in milliseconds
```

## Setup

**Dashboard (VPS):**
```bash
./deploy/deploy-dashboard.sh lyrics.yourdomain.org
```
Then set the shared password and proxy config it prints at the end.

**Worker (church PC),** from an administrator PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File worker\setup-windows.ps1 `
    -Url "https://lyrics.yourdomain.org" -Token "<worker token>"
```
Finds the ffmpeg and yt-dlp already on that machine and records their paths
rather than changing PATH, installs a real Python if only the Windows Store
stub is present, adds CUDA PyTorch, Whisper, Demucs and EasyOCR, then registers
a task that runs as SYSTEM at boot — the machine has no auto-login, so a task
tied to the signed-in session would not start after a restart.

**Development (Mac/Linux):**
```bash
python3 -m venv .venv && .venv/bin/pip install pillow numpy flask
echo "PEXELS_API_KEY=..." > .env
.venv/bin/python scripts/fetch_footage.py
```

## Reading the words

OCR on lyric-video type fails in ways that are systematic rather than random,
so they are repaired rather than reviewed. Everything here came off a real
render of a real song:

| fault | example | how it is fixed |
|---|---|---|
| words out of reading order | `Goodness God of` | fragments grouped into rows by median glyph height, then ordered left to right |
| the uploader's title card read as a lyric | `Instrumental Cover with lyrics` | cards in the first 30s checked for branding words and for the unreadability OCR makes of logo type |
| a letter replaced by a symbol | `goodness of €od` | named repairs, then a whitelist of characters a lyric may contain — anything else is dropped |
| c read as g | `throuch`, `nichts`, `cood`, `runninc` | a substitution is accepted **only** when the word as read is not vocabulary and the result is |

That last one needs vocabulary rather than a rule, because `c` is a legal
letter in all four positions. The ordering is the whole safety argument:
`cave`, `cold`, `class` and `come` are all in `engine/data/lyric_words.txt`,
so none of them can become `gave`, `gold`, `glass` or `gome`. An unknown word
is left alone.

The list is curated rather than a system dictionary, deliberately: web2 carries
`throuch` as an archaic spelling, so the very word needing repair would have
counted as already correct.

## The `.lyr` format

What the review step edits. Deliberately plain, so it can be fixed quickly:

```
# Song Title
[00:12.40 -> 00:16.80] first line as it should appear
[00:16.90 -> 00:21.10] second line
                       continued on a second display row
```

An indented continuation row becomes a literal line break on screen, so
whoever is proofreading controls where lines split without touching timings.

## Backgrounds

Renders use stock footage, but only footage a person has actually watched.
Nothing reaches a screen on trust:

- `scripts/fetch_footage.py` downloads candidates **for review only**, and
  catalogues every one with `approved=False`
- `for_mood()` will not return an unapproved clip, so a fetch cannot put
  anything on screen by itself
- a clip turned down is recorded in `assets/footage/rejected.json`, deleted
  from disk, and skipped by every future fetch. The library is built by
  keyword search and the same keyword returns the same results, so without
  this a re-fetch quietly re-catalogues what was just rejected — being asked
  twice about the same clip is how people stop answering

Clips are catalogued under a mood, and a theme prefers its own mood's clips.
Mood is a preference rather than a requirement: when a mood has none, any
approved clip is used instead. A moving backdrop from the reviewed pool beats
a flat colour, and the scrim makes the difference invisible anyway.

**The scrim** is a flat wash laid over the footage before the type goes on.
It settles the contrast under the lyrics and stops the backdrop reading as a
video — a clip anyone can follow is a clip whose loop point they will notice,
and a congregation watching the background is a congregation not singing.

Its colour is the theme's own ground, not simply black. `morning-light` sets
its lyrics in navy, so a black wash would leave dark type on a dark backdrop;
it is pushed toward cream instead and quietens exactly as much. Five themes go
dark, that one goes light, and the six looks stay distinct.

The prepared clips are tracked in git rather than ignored. The render machine
has no other way to get them, and `for_mood()` skipping a clip whose file is
missing means a silent fall back to a flat plate — no error, no log line, on a
machine nobody is watching.

## Getting the source video

Uploading the file is the dependable route, and it is what the form leads with.
YouTube declines to serve a great deal of music to anything that is not a
browser — two different uploads of the same worship song were refused from the
render machine while a control video downloaded fine seconds later. Links still
work for anything not blocked.

Authenticating around that was deliberately NOT done: the obvious credentials
on the render machine belong to the church's own Google account, which owns
the channel the Sunday service streams to.

## Timing

The one thing that cannot be wrong. A singer watching for their line mid-service
cannot recover from a late cue, so it is measured rather than asserted:

```bash
python scripts/verify_timing.py SOURCE.mp4 lyrics.lyr OUTPUT.mp4
```

Current measurement on the reference song:

| | |
|---|---|
| extraction (`.lyr` vs source) | 1.3 ms mean, 100% within one source frame |
| render (output vs `.lyr`) | 33.3 ms, constant — a fixed one-frame offset |
| **end to end** | **~35 ms**, inside the ~60 ms threshold of perception |

The residual frame is *early*, deliberately. Early is harmless; late is what
makes someone come in behind the music.

## Not during a service

The render machine is also the livestream machine, so `worker/guard.py` holds
three independent protections, all failing closed:

- **the clock** — no automatic work Sunday 10:40–12:30 or Wednesday 18:30–20:30
- **the process** — OBS running means a stream may be live whatever the clock says
- **the encoder** — while OBS is up, software encoding only, at reduced priority,
  so a render can never take one of the card's finite NVENC sessions

A person can override the clock (`--ignore-services`) — songs often arrive the
morning of. Nobody can override the stream protections.

## Notes

- **Licensing.** Projecting lyrics needs a CCLI (or equivalent) licence. This
  tool makes it easy to do a lot more of it; the licence still has to cover it.
- **Stock footage** is Pexels, free for commercial use with no attribution
  required. `engine.footage.credits()` prints the full list anyway.
- **Retention.** Finished videos are pruned from the VPS after 45 days to keep
  its small disk healthy. The church PC keeps every master.
- **Render time** is roughly 1.4× the song's length on the Mac; the 3060 Ti's
  NVENC is faster. A five-minute song is well under ten minutes either way.
