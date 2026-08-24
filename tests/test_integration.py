#!/usr/bin/env python3
"""End-to-end: a real worker against a real dashboard over real HTTP.

Everything else is unit-tested in isolation, which cannot catch the failures
that actually strand a job on a Sunday morning — a token that doesn't match, a
multipart upload the server rejects, a stage transition that leaves a job
un-claimable. So this starts the Flask app on a port, points the worker at it,
and pushes one tiny job all the way from "queued" to a downloadable file.

    python tests/test_integration.py

Kept out of the pytest run because it renders a video (a few seconds of one).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = "  \033[32mok\033[0m", "  \033[31mFAILED\033[0m"
_failures = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL}  {label}"
          + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        _failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def make_fixture(work: Path) -> tuple:
    """A 6-second tone plus three timed lines — enough to exercise everything."""
    audio = work / "audio.m4a"
    subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
         "-y", "-f", "lavfi", "-i", "sine=frequency=196:duration=6",
         "-c:a", "aac", "-b:a", "128k", str(audio)], check=True)
    lyrics = "\n".join([
        "# Integration Test",
        "[00:00.60 -> 00:02.60] first line of the test",
        "[00:02.80 -> 00:04.60] a second line that is long enough to wrap nicely",
        "[00:04.80 -> 00:05.90] and the last one",
    ]) + "\n"
    return audio, lyrics


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp(prefix="hopewell-integration-"))
    data_dir = tmp / "dashboard"
    work_dir = tmp / "worker"
    sunday = tmp / "sunday"

    print(f"\nworkspace: {tmp}\ndashboard: {base}\n")

    env = dict(os.environ, HOPEWELL_DATA=str(data_dir), PORT=str(port),
               PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "dashboard" / "app.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        if not wait_for(f"{base}/healthz"):
            out = ""
            if server.poll() is not None and server.stdout:
                out = server.stdout.read()[-1500:]
            print(f"{FAIL}  dashboard never came up\n{out}")
            return 1
        check("dashboard starts and answers /healthz", True)

        token = (data_dir / "worker-token.txt").read_text().strip()
        check("worker token was generated", bool(token))

        # --- the API rejects an unauthenticated worker --------------------
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{base}/api/claim", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"}), timeout=10)
            check("unauthenticated /api/claim is refused", False, "it succeeded")
        except urllib.error.HTTPError as exc:
            check("unauthenticated /api/claim is refused", exc.code == 401,
                  f"got {exc.code}")

        # --- queue a job through the same form the team uses --------------
        audio, lyrics = make_fixture(tmp)
        body = urllib.parse.urlencode({
            "title": "Integration Test",
            "source": "lyric_video",
            "source_ref": str(audio),
            "theme": "navy-minimal",
        }).encode()
        req = urllib.request.Request(f"{base}/new", data=body, method="POST")
        opener = urllib.request.build_opener(NoRedirect())
        resp = opener.open(req, timeout=15)
        location = resp.headers.get("Location", "")
        job_id = location.rsplit("/", 1)[-1]
        check("queueing a song through the web form", bool(job_id), location)

        api = Api(base, token)

        # --- pretend prepare() already ran; approve straight to render ----
        # (prepare() on a real source needs yt-dlp/tesseract and minutes; the
        #  OCR path has its own tests. What matters here is the handshake.)
        claimed = api.claim("integration")
        check("worker claims the queued job", claimed and claimed["id"] == job_id)
        api.patch(job_id, stage="review", lyrics=lyrics, title="Integration Test")

        approve = urllib.parse.urlencode({
            "lyrics": lyrics, "theme": "navy-minimal", "title": "Integration Test",
        }).encode()
        opener.open(urllib.request.Request(
            f"{base}/job/{job_id}/approve", data=approve, method="POST"), timeout=15)
        check("approving from the review screen", api.get(job_id)["stage"] == "approved")

        # --- the worker does the render for real --------------------------
        job_work = work_dir / job_id
        job_work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio, job_work / "audio.m4a")

        print("\n  rendering (a few seconds of video)…")
        worker = subprocess.run(
            [sys.executable, str(ROOT / "worker" / "worker.py"),
             "--url", base, "--token", token, "--once",
             "--work-dir", str(work_dir), "--sunday-dir", str(sunday),
             "--name", "integration"],
            capture_output=True, text=True, timeout=900)
        if worker.returncode != 0:
            print(worker.stdout[-2500:])
            print(worker.stderr[-1500:])
        check("worker runs the render and uploads", worker.returncode == 0)

        final = api.get(job_id)
        check("job reaches 'done'", final["stage"] == "done",
              f"{final['stage']} — {final.get('error', '')}")
        check("finished file was uploaded", final["output_bytes"] > 20_000,
              f"{final['output_bytes']} bytes")

        # --- the file really is downloadable and really is a video --------
        if final["output_bytes"]:
            got = tmp / "downloaded.mp4"
            with urllib.request.urlopen(f"{base}/job/{job_id}/download",
                                        timeout=60) as r, got.open("wb") as fh:
                shutil.copyfileobj(r, fh)
            check("download returns the same bytes",
                  got.stat().st_size == final["output_bytes"])
            probe = subprocess.run(
                [shutil.which("ffprobe") or "ffprobe", "-v", "error",
                 "-show_entries", "stream=codec_name,width,height",
                 "-of", "json", str(got)], capture_output=True, text=True)
            streams = json.loads(probe.stdout or "{}").get("streams", [])
            names = {s.get("codec_name") for s in streams}
            check("downloaded file is a real h264+aac video",
                  "h264" in names and "aac" in names, str(names))
            check("rendered at 1080p",
                  any(s.get("height") == 1080 for s in streams))

        check("a copy was kept on the local machine",
              any(sunday.glob("*.mp4")))

    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failures:
        print(f"\033[31m{len(_failures)} check(s) failed:\033[0m "
              + ", ".join(_failures))
        return 1
    print("\033[32mall integration checks passed\033[0m")
    return 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep 302s so the job id in the Location header can be read."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        return fp
    http_error_301 = http_error_303 = http_error_307 = http_error_302


class Api:
    def __init__(self, base: str, token: str):
        self.base, self.token = base, token

    def _call(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
        return json.loads(body) if body else {}

    def claim(self, worker):
        return self._call("/api/claim", "POST", {"worker": worker}).get("job")

    def patch(self, job_id, **fields):
        return self._call(f"/api/job/{job_id}", "PATCH", fields)

    def get(self, job_id):
        return self._call(f"/api/job/{job_id}", "PATCH", {})["job"]


if __name__ == "__main__":
    raise SystemExit(main())
