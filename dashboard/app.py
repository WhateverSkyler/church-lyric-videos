"""The dashboard the praise team uses, plus the API the church PC polls.

Runs on the VPS, not on the church computer, so that:

  * the team can queue songs any day of the week from a phone, whether or not
    the church machine happens to be awake
  * nothing has to be opened on the church's network — the worker only makes
    OUTBOUND https requests, so there is no port forwarding and no firewall
    rule for anyone to maintain

Two separate ways in, deliberately:

  people   nginx basic auth in front of the whole UI. That is the church's
           existing pattern on this box and it is what "one shared password"
           actually means in practice.
  worker   a bearer token on /api/*, so a leaked page password never lets
           somebody drive the render machine, and the token can be rotated
           without telling the whole team a new password.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.store import Store  # noqa: E402

DATA_DIR = Path(os.environ.get("HOPEWELL_DATA", ROOT / "work" / "dashboard"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "jobs.db"
#: How long finished videos stay downloadable here before being pruned.
RETENTION_DAYS = int(os.environ.get("HOPEWELL_RETENTION_DAYS", "45"))
MAX_UPLOAD_MB = int(os.environ.get("HOPEWELL_MAX_UPLOAD_MB", "600"))

MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
store = Store(DB_PATH)


def worker_token() -> str:
    token = os.environ.get("HOPEWELL_WORKER_TOKEN", "").strip()
    if not token:
        # Generated once and kept beside the database, so a fresh install
        # works without hand-editing anything, and the value survives restarts.
        path = DATA_DIR / "worker-token.txt"
        if path.is_file():
            token = path.read_text().strip()
        else:
            token = secrets.token_urlsafe(32)
            path.write_text(token)
            path.chmod(0o600)
    return token


def require_worker() -> None:
    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, worker_token()):
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
                           themes=themes_list())


@app.get("/new")
def new_job_form():
    return render_template("new.html", themes=themes_list())


@app.post("/new")
def create_job():
    form = request.form
    source_ref = (form.get("source_ref") or "").strip()
    if not source_ref:
        return render_template("new.html", themes=themes_list(),
                               error="Paste a link to the song first."), 400

    source = form.get("source", "lyric_video")
    original_ref = (form.get("original_ref") or "").strip()
    if source == "instrumental" and not original_ref:
        return render_template(
            "new.html", themes=themes_list(),
            error="For an instrumental, also paste a link to the original "
                  "recording with vocals — that is where the timings come from."), 400

    job_id = store.create(
        title=(form.get("title") or "").strip(),
        artist=(form.get("artist") or "").strip(),
        source=source,
        source_ref=source_ref,
        original_ref=original_ref,
        theme=form.get("theme", "cinematic-warm"),
        requested_by=current_user(),
        stage="queued",
    )
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/job/<job_id>")
def job_detail(job_id: str):
    job = store.get(job_id)
    if not job:
        abort(404)
    return render_template("job.html", job=job, themes=themes_list())


@app.post("/job/<job_id>/approve")
def approve(job_id: str):
    job = store.get(job_id)
    if not job:
        abort(404)
    if job["stage"] != "review":
        return redirect(url_for("job_detail", job_id=job_id))
    store.update(job_id,
                 lyrics=request.form.get("lyrics", job["lyrics"]),
                 theme=request.form.get("theme", job["theme"]),
                 title=(request.form.get("title") or job["title"]).strip(),
                 stage="approved", claimed_by="", claimed_at=0, progress="")
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
               "alignment_confidence", "theme", "progress"}
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


if __name__ == "__main__":
    print(f"worker token: {worker_token()}")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5060")),
            debug=bool(os.environ.get("HOPEWELL_DEBUG")))
