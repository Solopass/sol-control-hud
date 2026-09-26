"""One GPU job at a time across processes (chain runner, `note`, ...).

A byte-range lock on a lock file (msvcrt on Windows, fcntl elsewhere). The OS drops the lock when the holder
exits or crashes, so it can never stay stuck. The holder writes its pid + what it is doing into the file for humans.
Held per model call, not per run: a long chain and a `note` take turns instead of one waiting for hours.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

LOCK_PATH = Path(os.environ.get("SOL_GPU_LOCK", r"D:\AI\Cache\llm\gpu.lock"))

if os.name == "nt":
    import msvcrt

    def _try(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:  # pragma: no cover - tests run on Windows
    import fcntl

    def _try(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _release(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class LockTimeout(Exception):
    pass


def holder(path: Path = LOCK_PATH) -> dict | None:
    """Who holds (or last held) the lock, as written by them; None if unknown."""
    try:
        with open(path, "rb") as f:
            f.seek(1)  # byte 0 is the locked byte; Windows refuses to read it while held
            text = f.read().decode("utf-8", "replace").strip("\x00 \r\n")
        return json.loads(text) if text else None
    except (OSError, ValueError):
        return None


def is_held(path: Path = LOCK_PATH) -> bool:
    """True while some process holds the GPU lock. The lock *file* stays after use, so its existence means nothing;
    this tries the lock without waiting and releases it at once (a waiting job just retries a second later)."""
    if not path.exists():
        return False
    try:
        fh = open(path, "a+b")
    except OSError:
        return False
    try:
        if _try(fh):
            _release(fh)
            return False
        return True
    finally:
        fh.close()


@contextmanager
def gpu_lock(what: str, *, path: Path = LOCK_PATH, timeout: float | None = None,
             should_stop: Callable[[], bool] | None = None, on_wait: Callable[[dict | None], None] | None = None,
             poll: float = 1.0):
    """Wait for the GPU, run the body, release. `should_stop()` true while waiting -> raise LockTimeout (e.g. cancel)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    started = time.monotonic()
    told = False
    try:
        while not _try(fh):
            if not told and on_wait:
                on_wait(holder(path)); told = True
            if should_stop and should_stop():
                raise LockTimeout("stopped while waiting for the GPU")
            if timeout is not None and time.monotonic() - started > timeout:
                raise LockTimeout(f"GPU busy for over {timeout:.0f}s (held by {holder(path)})")
            time.sleep(poll)
        try:
            fh.seek(1); fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "what": what, "since": time.strftime("%Y-%m-%dT%H:%M:%S")}).encode())
            fh.flush()
            yield
        finally:
            _release(fh)
    finally:
        fh.close()
