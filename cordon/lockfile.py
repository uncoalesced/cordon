# Engineered by uncoalesced
"""Whole-line appends that two processes can make to one file at once.

A daemon thread and a stray command hook once wrote the same markers.jsonl together and one
line came out torn. Each append here is a single ``os.write`` of the encoded line through an
``O_APPEND`` descriptor, made while holding an OS lock on the file, so no reader ever sees half
a line from one writer followed by another's.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

LOCK_TIMEOUT_S = 2.0
_RETRY_S = 0.005

if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> bool:
        # Windows byte-range lock on byte 0 (locking past EOF is allowed); LK_NBLCK fails at once.
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_RETRY_S)

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _lock(fd: int) -> bool:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_RETRY_S)

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def append_line(path: Path, line: str) -> None:
    """Append one line (a trailing newline is added if missing). Raises OSError on failure.

    If the lock cannot be had within LOCK_TIMEOUT_S the line is still written: O_APPEND plus a
    single write keeps it whole on the local filesystems we target, and losing a marker is
    worse than the small chance of a torn one.
    """
    data = (line if line.endswith("\n") else line + "\n").encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        locked = _lock(fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            if locked:
                _unlock(fd)
    finally:
        os.close(fd)
