"""Write down an error the code deliberately carries on after, instead of dropping it with `except Exception: pass`.

Why: ticker.py's Desk/Away switch raised NameError on every use from its first commit, and an `except Exception: pass`
hid it until pyflakes found it on 10-08. Call `note("where")` inside such an `except`: the code still carries on
exactly as before, but the error lands in data\\swallowed.log, which the HUD health card reads.

Each place + error type is written at most once per REPEAT_S, so a handler on a 2 s loop can't flood the file.
note() never raises.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback

from .paths import DATA_DIR

LOG_FILE = DATA_DIR / "swallowed.log"
REPEAT_S = 3600.0
MAX_BYTES = 512 * 1024                 # then the file starts over as swallowed.log.old

_last: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def note(where: str) -> None:
    """Record the exception being handled right now (call it from inside an `except` block)."""
    try:
        exc = sys.exc_info()[1]
        if exc is None:
            return
        key = (where, type(exc).__name__)
        now = time.monotonic()
        with _lock:
            if now - _last.get(key, -1e9) < REPEAT_S:
                return
            _last[key] = now
        frame = traceback.extract_tb(exc.__traceback__)[-1:] if exc.__traceback__ else []
        at = f" ({frame[0].name} line {frame[0].lineno})" if frame else ""
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {where}{at}: {type(exc).__name__}: {str(exc)[:300]}\n"
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 - writing it down must never become a new failure
        return
