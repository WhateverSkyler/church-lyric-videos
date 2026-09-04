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

import http.cookiejar
import json
import os
import secrets
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

#: The shared password for the throwaway dashboard this test starts.
PASSWORD = "integration-test-password"
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


def multipart(fields: dict, file_field: str, path: Path) -> tuple:
    """Encode a form the way a browser does, so /new sees a real upload."""
    boundary = "----hopewell" + secrets.token_hex(8)
    sep = f"--{boundary}\r\n".encode()
    out = bytearray()
    for name, value in fields.items():
        out += sep
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    out += sep
    out += (f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    out += path.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp(prefix="hopewell-integration-"))
    data_dir = tmp / "dashboard"
    work_dir = tmp / "worker"
    sunday = tmp / "sunday"

    print(f"\nworkspace: {tmp}\ndashboard: {base}\n")

    env = dict(os.environ, HOPEWELL_DATA=str(data_dir), PORT=str(port),
               PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1",
               HOPEWELL_PASSWORD=PASSWORD)
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

        # --- sign in ------------------------------------------------------
        # Every page except /healthz, /login and the worker's own /api/* sits
        # behind one shared password. A test that skips this gets a 302 to
        # /login for each form POST, queues nothing, and still reads a
        # plausible-looking id out of the redirect - which is how this test
        # went on reporting a pass while doing nothing at all.
        # The session cookie is issued Secure, which is right in production
        # (the dashboard is only ever served over HTTPS) and fatal here: this
        # server is plain http on loopback, so a default jar would hold the
        # cookie and never send it back. The policy is relaxed rather than the
        # stored cookie, because Flask re-issues a permanent session cookie on
        # later responses - clearing the flag once would only last one request.
        # The server keeps running its real configuration.
        jar = http.cookiejar.CookieJar(InsecureOK())
        opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
        resp = opener.open(urllib.request.Request(
            f"{base}/login", data=urllib.parse.urlencode(
                {"password": PASSWORD}).encode(), method="POST"), timeout=15)
        check("signing in with the shared password", resp.status in (302, 303),
              f"got {resp.status}")



        # --- queue a job through the same form the team uses --------------
        # As a file upload, because that is what the form leads with and what
        # the team actually does: YouTube refuses most worship music to
        # anything that isn't a browser. It also exercises the multipart
        # handling this test exists to keep honest.
        audio, lyrics = make_fixture(tmp)
        body, content_type = multipart({
            "title": "Integration Test",
            "source": "lyric_video",
        }, "source_file", audio)
        req = urllib.request.Request(
            f"{base}/new", data=body, method="POST",
            headers={"Content-Type": content_type})
        resp = opener.open(req, timeout=15)
        location = resp.headers.get("Location", "")
        job_id = location.rsplit("/", 1)[-1]
        # Assert the shape of a real id, not merely that the string is
        # non-empty: "/login?next=/new" also parses to something truthy.
        check("queueing a song through the web form",
              location.startswith("/job/") and len(job_id) >= 8, location)

        api = Api(base, token)

        # --- prepare(), as the worker now does it -------------------------
        # A real prepare() needs yt-dlp/tesseract and minutes, and the OCR
        # path has its own tests; what matters here is the handshake. The
        # worker no longer stops at a review screen - it goes straight to
        # 'approved' - so this mirrors that rather than the retired gate.
        claimed = api.claim("integration")
        check("worker claims the queued job",
              bool(claimed) and claimed["id"] == job_id,
              f"claimed {claimed and claimed.get('id')!r}, queued {job_id!r}")
        api.patch(job_id, stage="approved", lyrics=lyrics,
                  title="Integration Test", error="")
        check("prepare hands the job straight to rendering",
              api.get(job_id)["stage"] == "approved")

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
            with opener.open(f"{base}/job/{job_id}/download",
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

            # The job page plays the video inline. That must serve as video,
            # not as a download, and must honour a range request - without
            # ranges the browser pulls the whole file before it plays a
            # second of it, which on a phone means nobody previews anything.
            req = urllib.request.Request(f"{base}/job/{job_id}/watch",
                                         headers={"Range": "bytes=0-2047"})
            with opener.open(req, timeout=30) as r:
                head = r.read()
                ctype = r.headers.get("Content-Type", "")
                disposition = r.headers.get("Content-Disposition", "")
                status = r.status
            check("the finished video streams inline for the player",
                  ctype.startswith("video/") and "attachment" not in disposition,
                  f"{status} {ctype} {disposition}")
            check("seeking works (range request honoured)",
                  status == 206 and len(head) == 2048,
                  f"status {status}, {len(head)} bytes")

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


class InsecureOK(http.cookiejar.DefaultCookiePolicy):
    """Allow a Secure cookie over plain http, for the loopback test server."""

    def return_ok_secure(self, cookie, request):
        return True


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
