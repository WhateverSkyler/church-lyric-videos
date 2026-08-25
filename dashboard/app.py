"""The dashboard the praise team uses, plus the API the church PC polls.

Runs on the VPS, not on the church computer, so that:

  * the team can queue songs any day of the week from a phone, whether or not
    the church machine happens to be awake
  * nothing has to be opened on the church's network — the worker only makes
    OUTBOUND https requests, so there is no port forwarding and no firewall
    rule for anyone to maintain

Two separate ways in, deliberately:

  people   one shared password on a normal sign-in page, held in a long-lived
           session cookie. Deliberately not HTTP basic auth: that always
           demands a username as well, and a second field nobody needs is the
           kind of friction that ends with the password on a sticky note.
  worker   a bearer token on /api/*, so a leaked page password never lets
           somebody drive the render machine, and the token can be rotated
           without telling the whole team a new password.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import timedelta
import time
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.store import Store  # noqa: E402
from dashboard.validate import (check_lyrics, check_new_song,  # noqa: E402
                                Errors)

# Absolute, always. Flask's send_from_directory resolves a relative directory
# against the app root (dashboard/), so a relative HOPEWELL_DATA uploads files
# to one place and then 404s looking for them in another.
DATA_DIR = Path(os.environ.get("HOPEWELL_DATA", ROOT / "work" / "dashboard")).resolve()
MEDIA_DIR = DATA_DIR / "media"
#: Videos the team uploads instead of pasting a link.
SOURCE_DIR = DATA_DIR / "sources"
DB_PATH = DATA_DIR / "jobs.db"
#: How long finished videos stay downloadable here before being pruned.
RETENTION_DAYS = int(os.environ.get("HOPEWELL_RETENTION_DAYS", "45"))
MAX_UPLOAD_MB = int(os.environ.get("HOPEWELL_MAX_UPLOAD_MB", "600"))

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
SOURCE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
store = Store(DB_PATH)


def worker_token() -> str:
    token = os.environ.get("HOPEWELL_WORKER_TOKEN", "").strip()
    if token:
        return token

    # Generated once and kept beside the database, so a fresh install works
    # without hand-editing anything and the value survives restarts.
    path = DATA_DIR / "worker-token.txt"
    if path.is_file():
        return path.read_text().strip()

    # Created with O_EXCL so that when gunicorn's workers race on the very
    # first request, exactly one wins and the loser reads what the winner
    # wrote. A plain write would leave the two processes disagreeing about the
    # token, and roughly half of the church PC's requests would be rejected.
    candidate = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(candidate)
        return candidate
    except FileExistsError:
        return path.read_text().strip()


# --------------------------------------------------------------------------
# people signing in
# --------------------------------------------------------------------------

#: How long a browser stays signed in. Long on purpose: this is a shared
#: password for a volunteer team, and being asked to retype it every week is
#: the thing that gets a password written on a sticky note by the sound desk.
SESSION_DAYS = 120


def secret_key() -> bytes:
    """Signing key for the session cookie, generated once and kept."""
    path = DATA_DIR / "session-key.bin"
    if path.is_file():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key
    except FileExistsError:
        return path.read_bytes()


def password_hash() -> str:
    """The stored hash of the shared password, seeded on first run."""
    path = DATA_DIR / "password.hash"
    if path.is_file():
        return path.read_text().strip()
    from werkzeug.security import generate_password_hash

    value = generate_password_hash(
        os.environ.get("HOPEWELL_PASSWORD", "PraiseTeam1"))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        return value
    except FileExistsError:
        return path.read_text().strip()


def set_password(new: str) -> None:
    from werkzeug.security import generate_password_hash

    path = DATA_DIR / "password.hash"
    path.write_text(generate_password_hash(new))
    path.chmod(0o600)


def signed_in() -> bool:
    return bool(session.get("in"))


@app.before_request
def gate():
    """One password for the whole site. The worker API is exempt.

    /api/* authenticates with a bearer token instead, so that the shared
    password the team knows can never be used to drive the render machine.
    """
    path = request.path
    if path.startswith("/api/") or path in ("/healthz", "/login"):
        return None
    if path.startswith("/static/"):
        return None
    if not signed_in():
        return redirect(url_for("login", next=request.full_path
                                if request.query_string else request.path))
    return None


@app.get("/login")
def login():
    if signed_in():
        return redirect(url_for("index"))
    return render_template("login.html", error="")


@app.post("/login")
def do_login():
    from werkzeug.security import check_password_hash

    supplied = request.form.get("password", "")
    if check_password_hash(password_hash(), supplied):
        session.permanent = True
        session["in"] = True
        target = request.args.get("next") or url_for("index")
        # Only ever redirect within this site.
        if not target.startswith("/"):
            target = url_for("index")
        return redirect(target)
    return render_template("login.html",
                           error="That password isn't right. Try again."), 401


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def require_worker() -> None:
    # worker_token() is resolved FIRST, not inside the condition. Written the
    # other way round, `not supplied` short-circuits on an unauthenticated
    # request and the token is never generated — leaving no way to read the
    # token off a fresh install without already possessing it.
    expected = worker_token()
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        abort(401)


#: Theme metadata is read from a small JSON file rather than by importing
#: engine.themes. That import pulls in numpy and Pillow for the rendering code,
#: which has no business on a 2 GB VPS with ~500 MB free that never renders
#: anything. scripts/export_themes.py regenerates the file.
THEMES_JSON = Path(__file__).resolve().parent / "themes.json"
_THEMES_CACHE: list = []


def themes_list() -> list:
    global _THEMES_CACHE
    if _THEMES_CACHE:
        return _THEMES_CACHE
    if THEMES_JSON.is_file():
        _THEMES_CACHE = json.loads(THEMES_JSON.read_text(encoding="utf-8"))
        return _THEMES_CACHE
    try:
        from engine.themes import listing

        _THEMES_CACHE = listing()
    except ImportError:
        # Never let a missing render dependency take the queue offline.
        _THEMES_CACHE = [{"key": "cinematic-warm", "name": "Cinematic Warm",
                          "mood": "warm", "description": ""}]
    return _THEMES_CACHE


#: The only key changes offered. Beyond a fourth either way the artefacts
#: are audible enough that a differently-keyed track is the better answer.
TRANSPOSE_CHOICES = list(range(-5, 6))


def transpose_value(raw, fallback: int = 0) -> int:
    """Read a key change off a form, clamped to what is actually offered."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value in TRANSPOSE_CHOICES else fallback


def current_user() -> str:
    """Whoever nginx authenticated, when it passes the name through."""
    return (request.headers.get("X-Forwarded-User")
            or request.remote_user
            or request.headers.get("X-Requested-By", "")
            or "team")


# ==========================================================================
# the web UI
# ==========================================================================


@app.get("/")
def index():
    jobs = store.list(limit=120)
    order = {"review": 0, "rendering": 1, "queued": 2, "fetching": 2,
             "extracting": 2, "approved": 2, "failed": 3, "done": 4,
             "expired": 5}
    jobs.sort(key=lambda j: (order.get(j["stage"], 9), -j["created_at"]))
    return render_template("index.html", jobs=jobs, counts=store.counts(),
                           themes=themes_list(), transposes=TRANSPOSE_CHOICES)


@app.get("/new")
def new_job_form():
    return render_template("new.html", themes=themes_list(),
                           transposes=TRANSPOSE_CHOICES,
                           errors=Errors(), values={})


def _save_upload(job_id: str, upload) -> str:
    """Store an uploaded source video. Returns the stored filename."""
    safe = "".join(c for c in Path(upload.filename).name
                   if c.isalnum() or c in " .-_()").strip()
    name = f"{job_id}-{safe or 'source.mp4'}"
    upload.save(SOURCE_DIR / name)
    return name


@app.post("/new")
def create_job():
    upload = request.files.get("source_file")
    has_file = bool(upload and upload.filename)
    values, errors = check_new_song(request.form, store.list(limit=200),
                                    TRANSPOSE_CHOICES,
                                    uploaded_name="pending" if has_file else "")
    if errors:
        # Everything typed is handed straight back, so nobody has to retype a
        # link on a phone because one field was wrong.
        return render_template("new.html", themes=themes_list(),
                               transposes=TRANSPOSE_CHOICES,
                               errors=errors, values=values), 400

    # A look is chosen for them. Picking one was the single most laboured
    # decision on the form and nobody minded which they got, so it rotates.
    values["theme"] = "random"
    job_id = store.create(requested_by=current_user(), stage="queued", **values)
    if has_file:
        # Named after the job, so the file can only be reached once the job it
        # belongs to exists.
        stored = _save_upload(job_id, upload)
        store.update(job_id, source_ref=f"upload:{stored}")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/job/<job_id>")
def job_detail(job_id: str):
    job = store.get(job_id)
    if not job:
        abort(404)
    return render_template("job.html", job=job, themes=themes_list(),
                           transposes=TRANSPOSE_CHOICES, errors=Errors())


@app.post("/job/<job_id>/approve")
def approve(job_id: str):
    job = store.get(job_id)
    if not job:
        abort(404)
    if job["stage"] != "review":
        return redirect(url_for("job_detail", job_id=job_id))

    lyrics, _count, errors = check_lyrics(request.form.get("lyrics", job["lyrics"]))
    title = (request.form.get("title") or job["title"]).strip()
    if not title:
        errors.add("title", "Give the song a name so everyone can find it later.")

    if errors:
        # Show the edited text back, not the stored copy, or corrections are lost.
        job = dict(job, lyrics=lyrics, title=title,
                   theme=request.form.get("theme", job["theme"]),
                   transpose=transpose_value(request.form.get("transpose"),
                                             job["transpose"]))
        return render_template("job.html", job=job, themes=themes_list(),
                               transposes=TRANSPOSE_CHOICES, errors=errors), 400

    store.update(job_id,
                 lyrics=lyrics,
                 theme=request.form.get("theme", job["theme"]),
                 transpose=transpose_value(request.form.get("transpose"),
                                           job["transpose"]),
                 title=title,
                 stage="approved", claimed_by="", claimed_at=0, progress="",
                 error="")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/job/<job_id>/retry")
def retry(job_id: str):
    job = store.get(job_id)
    if not job:
        abort(404)
    # A failed job goes back to whichever half it died in.
    stage = "approved" if job["lyrics"] else "queued"
    store.update(job_id, stage=stage, error="", claimed_by="", claimed_at=0,
                 progress="")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/job/<job_id>/delete")
def delete(job_id: str):
    job = store.get(job_id)
    if job and job["output_name"]:
        (MEDIA_DIR / job["output_name"]).unlink(missing_ok=True)
    store.delete(job_id)
    return redirect(url_for("index"))


@app.get("/job/<job_id>/download")
def download(job_id: str):
    job = store.get(job_id)
    if not job or not job["output_name"]:
        abort(404)
    return send_from_directory(MEDIA_DIR, job["output_name"], as_attachment=True)


def audio_name(job_id: str) -> str:
    return f"{job_id}-audio.m4a"


@app.get("/job/<job_id>/audio")
def job_audio(job_id: str):
    """Serve the track so the browser can play it for tap-timing."""
    path = MEDIA_DIR / audio_name(job_id)
    if not path.is_file():
        abort(404)
    return send_from_directory(MEDIA_DIR, path.name, mimetype="audio/mp4")


@app.get("/job/<job_id>/tap")
def tap(job_id: str):
    """Re-time a song by tapping along to it.

    The fallback for when automatic timing has nothing to work from: a Phase 2
    song with no original recording to borrow from, or an alignment that came
    back with low confidence. Also the quickest fix when OCR read the words
    correctly but the timings drifted.
    """
    job = store.get(job_id)
    if not job:
        abort(404)
    has_audio = (MEDIA_DIR / audio_name(job_id)).is_file()
    return render_template("tap.html", job=job, has_audio=has_audio)


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, jobs=store.counts(), retention_days=RETENTION_DAYS)


# ==========================================================================
# the worker API
# ==========================================================================


@app.post("/api/claim")
def api_claim():
    require_worker()
    worker = (request.json or {}).get("worker", "unknown")
    job = store.claim_next(worker)
    if not job:
        return jsonify(job=None)
    return jsonify(job=job)


@app.patch("/api/job/<job_id>")
def api_update(job_id: str):
    require_worker()
    if not store.get(job_id):
        abort(404)
    allowed = {"stage", "error", "notes", "lyrics", "title", "artist",
               "alignment_confidence", "theme", "progress", "transpose"}
    fields = {k: v for k, v in (request.json or {}).items() if k in allowed}
    if fields:
        store.update(job_id, **fields)
    return jsonify(ok=True, job=store.get(job_id))


@app.post("/api/job/<job_id>/heartbeat")
def api_heartbeat(job_id: str):
    require_worker()
    store.heartbeat(job_id, (request.json or {}).get("progress", ""))
    return jsonify(ok=True)


@app.post("/api/job/<job_id>/upload")
def api_upload(job_id: str):
    require_worker()
    job = store.get(job_id)
    if not job:
        abort(404)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        abort(400, "no file")

    safe = "".join(c for c in Path(upload.filename).name
                   if c.isalnum() or c in " .-_()").strip()
    name = f"{job_id}-{safe or 'video.mp4'}"
    dest = MEDIA_DIR / name
    upload.save(dest)

    store.update(job_id, output_name=name, output_bytes=dest.stat().st_size,
                 stage="done", error="", progress="")
    # Opportunistic cleanup — no cron needed on the VPS for this.
    store.prune(RETENTION_DAYS, MEDIA_DIR)
    return jsonify(ok=True, name=name, bytes=dest.stat().st_size)


@app.post("/api/job/<job_id>/audio")
def api_upload_audio(job_id: str):
    """The worker posts the track here so the browser can tap-time against it.

    Audio only — a few MB per song rather than the couple of hundred a
    finished video costs, so keeping it for every job is cheap.
    """
    require_worker()
    if not store.get(job_id):
        abort(404)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        abort(400, "no file")
    dest = MEDIA_DIR / audio_name(job_id)
    upload.save(dest)
    return jsonify(ok=True, bytes=dest.stat().st_size)


@app.get("/api/job/<job_id>/source")
def api_source(job_id: str):
    """Hand the worker the file that was uploaded for this job."""
    require_worker()
    job = store.get(job_id)
    if not job:
        abort(404)
    ref = job.get("source_ref", "")
    if not ref.startswith("upload:"):
        abort(404, "this job has no uploaded file")
    name = ref.split(":", 1)[1]
    if not (SOURCE_DIR / name).is_file():
        abort(404, "the uploaded file is no longer on the server")
    return send_from_directory(SOURCE_DIR, name, as_attachment=True)


@app.get("/api/themes")
def api_themes():
    require_worker()
    return jsonify(themes=themes_list())


# --------------------------------------------------------------------------


@app.template_filter("ago")
def ago(ts: float) -> str:
    delta = max(0, time.time() - float(ts or 0))
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "m"),
                             (86400, 3600, "h"), (86400 * 30, 86400, "d")):
        if delta < limit:
            return f"{int(delta // div)}{unit} ago"
    return time.strftime("%d %b", time.localtime(ts))


@app.template_filter("size")
def size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


app.secret_key = secret_key()
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)
app.config.update(SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=not os.environ.get("HOPEWELL_DEBUG"))


if __name__ == "__main__":
    print(f"worker token: {worker_token()}")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5060")),
            debug=bool(os.environ.get("HOPEWELL_DEBUG")))
