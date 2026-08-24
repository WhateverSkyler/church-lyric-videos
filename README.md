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
                                        RTX 3060 Ti · OCR / Whisper / render
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

Either way it stops and asks a human to check the words before rendering
anything. Automatic transcription gets a word wrong now and then, and a wrong
word is far more embarrassing on a sanctuary screen than in a text box.

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

Backgrounds are real stock footage from Pexels (free for commercial use),
auto-graded and seamlessly looped. `random` picks one for you.

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
  background.py   procedural backdrops, for when no footage is available
  footage.py      Pexels sourcing, exposure grading, seamless looping
  ocr.py          Phase 1 — lyrics and timing off a source video
  align.py        Phase 2 — demucs + Whisper + chroma DTW
  compositor.py   the per-frame renderer
  render.py       encoder selection, logo placement, stills
  lyrics.py       the data model and the .lyr proofreading format
  pipeline.py     prepare() / render(), the two halves of a job
dashboard/        Flask queue + web UI (runs on the VPS)
worker/           the polling render worker (runs on the church PC)
deploy/           deployment scripts
```

## Setup

**Dashboard (VPS):**
```bash
./deploy/deploy-dashboard.sh lyrics.tristanaddi.com
```
Then set the shared password and proxy config it prints at the end.

**Worker (church PC),** from an administrator PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File worker\setup-windows.ps1 `
    -Url "https://lyrics.tristanaddi.com" -Token "<worker token>"
```
Installs Python, FFmpeg, yt-dlp, Tesseract and CUDA PyTorch, then registers a
scheduled task that starts at boot and restarts itself on failure.

**Development (Mac/Linux):**
```bash
python3 -m venv .venv && .venv/bin/pip install pillow numpy flask
echo "PEXELS_API_KEY=..." > .env
.venv/bin/python scripts/fetch_footage.py
```

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

## Notes

- **Licensing.** Projecting lyrics needs a CCLI (or equivalent) licence. This
  tool makes it easy to do a lot more of it; the licence still has to cover it.
- **Stock footage** is Pexels, free for commercial use with no attribution
  required. `engine.footage.credits()` prints the full list anyway.
- **Retention.** Finished videos are pruned from the VPS after 45 days to keep
  its small disk healthy. The church PC keeps every master.
- **Render time** is roughly 1.4× the song's length on the Mac; the 3060 Ti's
  NVENC is faster. A five-minute song is well under ten minutes either way.
