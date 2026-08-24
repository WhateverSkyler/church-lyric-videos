"""Keeps rendering from ever interfering with a live service.

The render machine is also the livestream machine. That single fact drives
everything here: a render must never take an encoder session, a CPU core, or a
window focus away from a service that is on the air. Getting this wrong is not
a slow render, it is a broken stream in front of the congregation.

Three independent protections, because any one of them can be wrong:

  the clock     a hard window around the service where automatic work stops
  the process   if OBS is running the stream may be live regardless of time
  the encoder   whenever OBS is up, software encoding only, so a render can
                never consume one of the card's finite NVENC sessions

All three fail CLOSED. If a check itself errors — WMI hiccup, permissions,
anything — the answer is "not safe", because the cost of a false stop is a
render that happens twenty minutes later, and the cost of a false go is a
service interruption.

A person can override the clock. Nobody can override the stream protections:
an explicit "render now" during a service still drops to software encoding at
low priority, because those exist to protect the stream, not the schedule.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

#: Weekday numbers as datetime.weekday() reports them.
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@dataclass(frozen=True)
class Window:
    """A recurring period when automatic rendering stops."""

    weekday: int
    start: dtime
    end: dtime
    label: str

    def contains(self, when: datetime) -> bool:
        return self.weekday == when.weekday() and self.start <= when.time() < self.end

    def next_start(self, after: datetime) -> datetime:
        days = (self.weekday - after.weekday()) % 7
        candidate = datetime.combine(after.date() + timedelta(days=days), self.start)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    def ends_at(self, when: datetime) -> datetime:
        return datetime.combine(when.date(), self.end)


#: Sunday service runs 11:00-12:10. The window opens at 10:40 because the
#: stream and its ISO recordings are started and checked before the service,
#: and closes at 12:30 to cover an overrun and teardown. Deliberately wider
#: than the service itself — the cost of stopping early is nil.
SERVICE_WINDOWS = (
    Window(SUN, dtime(10, 40), dtime(12, 30), "Sunday morning service"),
    Window(WED, dtime(18, 30), dtime(20, 30), "Wednesday evening"),
)

#: Processes that mean a broadcast may be live. Matched case-insensitively
#: against the executable name.
BROADCAST_PROCESSES = (
    "obs64", "obs32", "obs",
    "wirecast", "vmix", "xsplit",
    "zoom", "ffmpeg-stream",
)


@dataclass
class Verdict:
    """Whether work may proceed, and what it must give up if it does."""

    #: Automatic rendering allowed right now.
    safe: bool
    #: Human-readable explanation, always populated.
    reason: str
    #: True when a broadcast tool is running, whatever the clock says.
    broadcast_running: bool = False
    #: Hardware encoding is forbidden (a stream may own the NVENC sessions).
    force_software: bool = False
    #: Run below normal priority so the stream always wins the CPU.
    low_priority: bool = False
    #: When the current blocking window ends, if the clock is what blocked it.
    clear_at: datetime | None = None

    def describe(self) -> str:
        bits = [self.reason]
        if self.force_software:
            bits.append("software encoding only")
        if self.low_priority:
            bits.append("reduced priority")
        return "; ".join(bits)


# --------------------------------------------------------------------------


def running_processes() -> set:
    """Lowercased executable names currently running. Empty set on failure."""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=20)
            if out.returncode != 0:
                return set()
            names = set()
            for line in out.stdout.splitlines():
                if line.startswith('"'):
                    names.add(line.split('","')[0].lstrip('"').lower())
            return names
        out = subprocess.run(["ps", "-A", "-o", "comm="],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return set()
        return {os.path.basename(l.strip()).lower() for l in out.stdout.splitlines()}
    except (subprocess.SubprocessError, OSError):
        return set()


def broadcast_active() -> tuple:
    """(is_running, which). Fails closed: an unreadable process list is 'yes'."""
    names = running_processes()
    if not names:
        # Either nothing is running (impossible) or the query failed. Assume
        # the worst rather than risk encoding over a live stream.
        return True, "could not read the process list"
    for proc in BROADCAST_PROCESSES:
        for name in names:
            if name == f"{proc}.exe" or name == proc:
                return True, name
    return False, ""


def active_window(now: datetime, windows=SERVICE_WINDOWS) -> Window | None:
    for window in windows:
        if window.contains(now):
            return window
    return None


def check(now: datetime | None = None, windows=SERVICE_WINDOWS,
          override: bool = False) -> Verdict:
    """Decide whether rendering may proceed right now.

    Args:
        override: a person explicitly asked for this render. Lifts the clock
            restriction only — the stream protections still apply.
    """
    now = now or datetime.now()

    try:
        live, which = broadcast_active()
    except Exception:
        live, which = True, "process check failed"

    window = active_window(now, windows)

    # --- a broadcast tool is up ---------------------------------------
    if live:
        # Never hard-block on OBS alone: it is often left open all week. But
        # never let a render touch the GPU encoder or outrank it on CPU.
        return Verdict(
            safe=bool(override) or window is None,
            reason=(f"{which} is running, so the stream may be live"
                    if not window else
                    f"{which} is running during {window.label}"),
            broadcast_running=True,
            force_software=True,
            low_priority=True,
            clear_at=window.ends_at(now) if window else None,
        )

    # --- inside a service window --------------------------------------
    if window is not None:
        if override:
            return Verdict(
                safe=True,
                reason=f"manual override during {window.label}",
                force_software=True,
                low_priority=True,
                clear_at=window.ends_at(now),
            )
        return Verdict(
            safe=False,
            reason=f"paused for {window.label}",
            clear_at=window.ends_at(now),
        )

    return Verdict(safe=True, reason="clear")


def would_cross_window(now: datetime, estimated_seconds: float,
                       windows=SERVICE_WINDOWS) -> Window | None:
    """The window a render starting now would run into, if any.

    Starting a twenty-minute render at 10:35 on a Sunday is worse than
    refusing it: it is still encoding when the service goes live.
    """
    finish = now + timedelta(seconds=estimated_seconds)
    for window in windows:
        start = window.next_start(now)
        if now < start <= finish:
            return window
    return None


def estimate_seconds(song_seconds: float, hardware: bool) -> float:
    """Rough wall-clock for a render, from measured throughput.

    Measured: 316 s of video in 472 s with hardware encoding (~1.5x realtime).
    Software encoding on a 6-core desktop runs closer to 2.5x, and the
    estimate is deliberately pessimistic so the answer errs toward waiting.
    """
    factor = 1.6 if hardware else 2.8
    return song_seconds * factor + 90.0


def priority_args() -> list:
    """Command prefix that runs a child process below normal priority."""
    if platform.system() == "Windows":
        # start /B /BELOWNORMAL keeps it in the same console with no new window.
        return ["cmd", "/c", "start", "/B", "/BELOWNORMAL", "/WAIT"]
    return ["nice", "-n", "15"]


def lower_own_priority() -> None:
    """Drop this process's own priority, so ffmpeg children inherit it."""
    try:
        if platform.system() == "Windows":
            import ctypes

            # BELOW_NORMAL_PRIORITY_CLASS
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        else:
            os.nice(15)
    except Exception:
        pass


def summary(now: datetime | None = None) -> str:
    """One line for the worker log, so the reason is always visible."""
    verdict = check(now)
    if verdict.safe:
        return f"clear to render ({verdict.describe()})"
    when = verdict.clear_at.strftime("%H:%M") if verdict.clear_at else "later"
    return f"holding: {verdict.describe()} — resumes {when}"
