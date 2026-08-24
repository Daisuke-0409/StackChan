"""Writes a service's log itself, instead of letting PowerShell do it.

The scheduled tasks start these servers through a PowerShell wrapper and
append its output with `*>>`. That works, and it produced a log nobody
could read: PowerShell 5.1 writes redirected output as UTF-16, so a file
opened as UTF-8 (or by grep, or by tail) comes back as

    C o n n e c t i o n R e s e t E r r o r :   [ W i n E r r o r   1 0 0 5 4 ]

and the first line, written by something else, is UTF-8. One file, two
encodings, no tool that reads both. On 2026-08-24 the office body went
quiet and every attempt to look at the log needed a decoder written on
the spot -- while the answer was sitting in it in plain text.

Writing it from here fixes that by removing the translator. It also lets
the file be capped, which the redirect could never do: forwarder.log had
reached 73MB, growing 2.8MB a day, and 62% of it was stack traces.

    $env:TACHIKOMA_LOG_FILE = "C:\\...\\logs\\forwarder.log"
    python -u gateway/forwarder.py

Unset, nothing happens and output goes to the console as before -- so
running a server by hand still shows its output, and only the tasks take
this path.

A BOM is written at the head of each file on purpose. It is redundant for
grep and for Python, and it is the one thing that stops PowerShell's own
Get-Content from reading the Japanese in these logs as ANSI -- the same
trap the .env files hit, from the other side.
"""
from __future__ import annotations

import os
import sys
import threading

# Big enough to hold days of ordinary traffic, small enough to open in an
# editor when something has gone wrong.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

# forwarder.log.1 .. .3, then the oldest is dropped. Four files is a bit
# over a fortnight at the rate the forwarder writes once the stack traces
# are gone, which is longer than any question anyone has asked of them.
DEFAULT_KEEP = 3


class RotatingLog:
    """A file object accepting what print() and traceback writes into it.

    Rotation is checked on write rather than on a timer, because these
    processes can sit idle for hours and then say everything at once.
    """

    def __init__(self, path: str, max_bytes: int, keep: int) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._keep = keep
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._handle = self._open()

    def _open(self):
        new = not os.path.exists(self._path) or os.path.getsize(self._path) == 0
        handle = open(self._path, "a", encoding="utf-8", newline="\n")
        if new:
            handle.write("\ufeff")
            handle.flush()
        return handle

    def _rotate(self) -> None:
        self._handle.close()
        # Oldest first, so nothing is overwritten before it has been moved.
        for index in range(self._keep, 0, -1):
            older = f"{self._path}.{index}"
            newer = self._path if index == 1 else f"{self._path}.{index - 1}"
            if os.path.exists(older):
                os.remove(older)
            if os.path.exists(newer):
                os.replace(newer, older)
        self._handle = self._open()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            self._handle.write(text)
            self._handle.flush()
            if self._handle.tell() >= self._max_bytes:
                self._rotate()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.flush()

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


def install(env: dict[str, str] | None = None) -> RotatingLog | None:
    """Points stdout and stderr at the configured file. Returns it, or None.

    Both streams, because a traceback the interpreter prints on its own is
    exactly the thing worth keeping, and it goes to stderr.
    """
    env = os.environ if env is None else env
    path = env.get("TACHIKOMA_LOG_FILE", "").strip()
    if not path:
        return None
    try:
        max_bytes = int(env.get("TACHIKOMA_LOG_MAX_BYTES", str(DEFAULT_MAX_BYTES)))
        keep = int(env.get("TACHIKOMA_LOG_KEEP", str(DEFAULT_KEEP)))
    except ValueError:
        max_bytes, keep = DEFAULT_MAX_BYTES, DEFAULT_KEEP
    try:
        log = RotatingLog(path, max_bytes, max(keep, 0))
    except OSError as exc:
        # A log that cannot be opened must not stop the robot from working.
        print(f"log file {path} could not be opened ({type(exc).__name__}); "
              f"writing to the console instead", file=sys.stderr, flush=True)
        return None
    sys.stdout = log
    sys.stderr = log
    return log
