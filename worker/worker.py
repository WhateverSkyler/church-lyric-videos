#!/usr/bin/env python3
"""The render worker that lives on the church computer.

Polls the dashboard for work, does it on the local GPU, and posts the finished
video back. Every request it makes is OUTBOUND, so the church network needs no
port forwarding, no inbound firewall rule and no static IP — which is the whole
reason the queue lives on the VPS rather than here.

    python worker/worker.py --url https://lyrics.example.com --token <token>

Configuration can also come from worker/.env or the environment:

    HOPEWELL_URL,  HOPEWELL_WORKER_TOKEN,  HOPEWELL_SUNDAY_DIR

Designed to be restarted at will: a job that dies mid-render is re-queued by
the dashboard once its claim goes stale, so crashing is never worse than slow.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys
import time
import traceback
from pathlib import Path

import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.lyrics import LyricTrack  # noqa: E402
from engine.pipeline import Job, Source, Stage, prepare, render  # noqa: E402
from engine.render import probe_duration  # noqa: E402
from worker import guard  # noqa: E402

POLL_SECONDS = 6.0
#: Progress is pushed at most this often, to keep the VPS's tiny database calm.
HEARTBEAT_SECONDS = 8.0


# --------------------------------------------------------------------------
# talking to the dashboard
# --------------------------------------------------------------------------


class Dashboard:
    def __init__(self, base_url: str, token: str, worker: str):
        self.base = base_url.rstrip("/")
        self.token = token
        self.worker = worker

    def _call(self, path: str, method: str = "GET", payload=None,
              timeout: float = 30.0):
        url = f"{self.base}{path}"
        data = None
        headers = {"Authorization": f"Bearer {self.token}",
                   "User-Agent": "hopewell-worker/1.0"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}

    def claim(self):
        return self._call("/api/claim", "POST", {"worker": self.worker}).get("job")

    def update(self, job_id: str, **fields):
        return self._call(f"/api/job/{job_id}", "PATCH", fields)

    def heartbeat(self, job_id: str, progress: str):
        try:
            self._call(f"/api/job/{job_id}/heartbeat", "POST",
                       {"progress": progress}, timeout=15.0)
        except Exception:
            # A missed heartbeat is not worth failing a render over; the claim
            # only goes stale after 45 minutes of total silence.
            pass

    def upload(self, job_id: str, path: Path, kind: str = "upload"):
        """Multipart upload, hand-rolled to avoid a requests dependency.

        `kind` is 'upload' for the finished video or 'audio' for the track the
        browser needs in order to tap-time.
        """
        boundary = f"----hopewell{int(time.time() * 1000):x}"
        name = path.name.encode("utf-8")
        head = (f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"file\"; "
                f"filename=\"{name.decode('utf-8')}\"\r\n"
                f"Content-Type: video/mp4\r\n\r\n").encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        size = path.stat().st_size

        with path.open("rb") as fh:
            body = _ChainedStream([head, fh, tail], len(head) + size + len(tail))
            req = urllib.request.Request(
                f"{self.base}/api/job/{job_id}/{kind}", data=body, method="POST",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(body.length),
                    "User-Agent": "hopewell-worker/1.0",
                })
            with urllib.request.urlopen(req, timeout=1800) as resp:
                return json.loads(resp.read().decode("utf-8"))


class _ChainedStream:
    """Reads several sources in order, so a big file never loads into memory."""

    def __init__(self, parts, length: int):
        self.parts = list(parts)
        self.length = length
        self._i = 0

    def read(self, size: int = -1) -> bytes:
        while self._i < len(self.parts):
            part = self.parts[self._i]
            chunk = part[:size] if isinstance(part, bytes) else part.read(size)
            if isinstance(part, bytes):
                self.parts[self._i] = part[len(chunk):]
                if not self.parts[self._i]:
                    self._i += 1
                if chunk:
                    return chunk
                continue
            if chunk:
                return chunk
            self._i += 1
        return b""


# --------------------------------------------------------------------------


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def describe_gpu() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "no NVIDIA GPU detected (renders will use the CPU and be slow)"
    try:
        import subprocess

        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 else "unknown GPU"
    except Exception:
        return "unknown GPU"


def load_env() -> None:
    env = Path(__file__).resolve().parent / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# --------------------------------------------------------------------------


class Worker:
    def __init__(self, api: Dashboard, workroot: Path, sunday_dir: Path,
                 respect_services: bool = True):
        self.api = api
        self.workroot = workroot
        self.sunday_dir = sunday_dir
        self.respect_services = respect_services
        self.workroot.mkdir(parents=True, exist_ok=True)
        self.sunday_dir.mkdir(parents=True, exist_ok=True)
        self._last_beat = 0.0
        self._held_reason = ""

    def _progress(self, job_id: str):
        def report(stage: str, n: int, total: int):
            now = time.time()
            if now - self._last_beat < HEARTBEAT_SECONDS:
                return
            self._last_beat = now
            text = (f"{stage} {n}/{total} ({100.0 * n / total:.0f}%)"
                    if total else f"{stage} {n}")
            self.api.heartbeat(job_id, text)
        return report

    def handle(self, row: dict) -> None:
        job_id = row["id"]
        stage = row["stage"]
        workdir = self.workroot / job_id
        log(f"claimed {job_id} '{row.get('title') or 'untitled'}' at stage {stage}")

        job = Job(
            id=job_id,
            title=row.get("title", ""),
            artist=row.get("artist", ""),
            source=Source(row.get("source", "lyric_video")),
            source_ref=row.get("source_ref", ""),
            original_ref=row.get("original_ref", ""),
            theme=row.get("theme", "cinematic-warm"),
            transpose=int(row.get("transpose") or 0),
        )

        if stage in ("fetching", "extracting"):
            self._do_prepare(job, workdir)
        elif stage == "rendering":
            self._do_render(job, workdir, row)
        else:
            log(f"  nothing to do for stage {stage}")

    # -- half one ------------------------------------------------------

    def _do_prepare(self, job: Job, workdir: Path) -> None:
        job = prepare(job, workdir, self._progress(job.id))
        if job.stage == Stage.FAILED:
            log(f"  prepare failed: {job.error}")
            self.api.update(job.id, stage="failed", error=job.error)
            return

        track = LyricTrack.load(Path(job.lyrics_path))
        log(f"  recovered {len(track)} lines -> awaiting review")

        # Send the track up too, so the review screen can offer tap-timing.
        # Audio is a few MB against the couple of hundred a finished video
        # costs, so it is worth doing for every job rather than on demand.
        try:
            self.api.upload(job.id, Path(job.audio_path), kind="audio")
        except Exception as exc:
            log(f"  note: could not upload the track for tap-timing ({exc})")

        self.api.update(
            job.id,
            stage="review",
            lyrics=Path(job.lyrics_path).read_text(encoding="utf-8"),
            title=job.title or track.title,
            notes=job.notes,
            alignment_confidence=job.alignment_confidence,
            error="",
        )

    # -- half two ------------------------------------------------------

    def _do_render(self, job: Job, workdir: Path, row: dict) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        # The reviewed text is authoritative — it may have been corrected in
        # the browser since prepare() wrote the file.
        lyrics_file = workdir / "approved.lyr"
        lyrics_file.write_text(row.get("lyrics", ""), encoding="utf-8")
        job.lyrics_path = str(lyrics_file)

        audio = workdir / "audio.m4a"
        if not audio.is_file():
            candidates = sorted(workdir.glob("*.m4a")) + sorted(workdir.glob("*.mp4"))
            if not candidates:
                msg = ("The downloaded audio is gone from this machine — "
                       "re-run it from the start.")
                log(f"  {msg}")
                self.api.update(job.id, stage="failed", error=msg)
                return
            audio = candidates[0]
        job.audio_path = str(audio)

        verdict = guard.check() if self.respect_services else guard.Verdict(True, "guard off")
        if verdict.broadcast_running or verdict.low_priority:
            # Never outrank a live stream for CPU. The renders that matter are
            # queued days ahead; the service is happening now.
            guard.lower_own_priority()
            log(f"  {verdict.describe()}")

        # Refuse to START work that would still be encoding when a service
        # begins. Finishing late is worse than starting late.
        if self.respect_services:
            try:
                song = probe_duration(Path(job.audio_path))
                # Ask what the encoder will ACTUALLY be, rather than assuming
                # hardware whenever a stream isn't running. NVENC can be
                # unusable for reasons that have nothing to do with OBS — a
                # driver too old for the build's NVENC API, for one — and an
                # optimistic estimate would let a render start that then runs
                # into a service.
                from engine.render import pick_encoder

                encoder, _ = pick_encoder(
                    allow_hardware=not verdict.force_software)
                estimate = guard.estimate_seconds(
                    song, hardware=encoder != "libx264")
                from datetime import datetime

                crossing = guard.would_cross_window(datetime.now(), estimate)
            except Exception:
                crossing = None
            if crossing is not None:
                log(f"  deferring — this render would still be going during "
                    f"{crossing.label}")
                self.api.update(job.id, stage="approved", claimed_by="",
                                claimed_at=0, progress="",
                                notes=(row.get("notes", "") +
                                       f"\nheld back so it would not run into "
                                       f"{crossing.label}").strip())
                return

        out_dir = workdir / "out"
        job = render(job, out_dir, self._progress(job.id),
                     force_software=verdict.force_software)
        if job.stage == Stage.FAILED:
            log(f"  render failed: {job.error}")
            self.api.update(job.id, stage="failed", error=job.error)
            return

        produced = Path(job.output_path)
        # Keep the master where the church can reach it on Sunday even if the
        # internet is down.
        local = self.sunday_dir / produced.name
        try:
            shutil.copy2(produced, local)
            log(f"  saved locally -> {local}")
        except OSError as exc:
            log(f"  WARNING could not copy to the Sunday folder: {exc}")

        log(f"  uploading {produced.name} ({produced.stat().st_size / 1e6:.0f} MB)")
        self.api.update(job.id, notes=job.notes, theme=job.theme)
        self.api.upload(job.id, produced)
        log("  done")

    # -- the loop ------------------------------------------------------

    def run_forever(self) -> None:
        idle_logged = False
        while True:
            if self.respect_services:
                verdict = guard.check()
                if not verdict.safe:
                    if self._held_reason != verdict.reason:
                        self._held_reason = verdict.reason
                        log(guard.summary())
                    time.sleep(60)
                    continue
                self._held_reason = ""

            try:
                row = self.api.claim()
            except urllib.error.HTTPError as exc:
                log(f"dashboard returned {exc.code} {exc.reason}"
                    + (" — check the worker token" if exc.code == 401 else ""))
                time.sleep(30)
                continue
            except (urllib.error.URLError, socket.timeout, OSError) as exc:
                log(f"cannot reach the dashboard ({exc}); retrying")
                time.sleep(20)
                continue

            if not row:
                if not idle_logged:
                    log("waiting for work")
                    idle_logged = True
                time.sleep(POLL_SECONDS)
                continue

            idle_logged = False
            try:
                self.handle(row)
            except Exception as exc:
                log(f"  unexpected failure: {exc}")
                traceback.print_exc()
                try:
                    self.api.update(row["id"], stage="failed", error=str(exc))
                except Exception:
                    pass


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description="Hopewell render worker")
    ap.add_argument("--url", default=os.environ.get("HOPEWELL_URL", ""))
    ap.add_argument("--token", default=os.environ.get("HOPEWELL_WORKER_TOKEN", ""))
    ap.add_argument("--sunday-dir",
                    default=os.environ.get("HOPEWELL_SUNDAY_DIR",
                                           str(Path.home() / "Hopewell Lyric Videos")))
    ap.add_argument("--work-dir", default=str(ROOT / "work" / "worker"))
    ap.add_argument("--name", default=f"{platform.node() or 'church-pc'}")
    ap.add_argument("--once", action="store_true", help="handle one job and exit")
    ap.add_argument("--ignore-services", action="store_true",
                    help="render even during a service. Stream protections "
                         "(software encoding, low priority) still apply.")
    args = ap.parse_args()

    if not args.url or not args.token:
        print("Need --url and --token (or HOPEWELL_URL / HOPEWELL_WORKER_TOKEN,\n"
              "or a worker/.env file containing them).")
        return 2

    log(f"worker '{args.name}' on {platform.system()} {platform.release()}")
    log(f"GPU: {describe_gpu()}")
    log(f"dashboard: {args.url}")
    log(f"finished videos: {args.sunday_dir}")
    log(guard.summary())

    api = Dashboard(args.url, args.token, args.name)
    worker = Worker(api, Path(args.work_dir), Path(args.sunday_dir),
                    respect_services=not args.ignore_services)

    if args.once:
        row = api.claim()
        if not row:
            log("nothing queued")
            return 0
        worker.handle(row)
        return 0

    try:
        worker.run_forever()
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
