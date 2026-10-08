"""How fast the local AI really answers, and what the watcher's spill auto-heal is doing. Both read logs only.

Why: on 2026-10-07 sol-fast answered at 23-28 tok/s instead of ~140 while the VRAM card said OK. Desktop apps held
5.7 GB of the card, Windows kept 0.62 GB of the model in system RAM, and that was below the 1 GB "evicted" line, so
nothing showed it. The router logs every answer's generation speed (llama.cpp `print_timing`), which is the number
that matters; the watcher (OBVLT tools/sol-llm-watch.ps1) logs when it heals a spill or waits to.

Nothing here talks to a model (never /slots: that counts as use and keeps it loaded).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

LLM_DIR = Path(r"D:\AI\Cache\llm")
ROUTER_LOG = LLM_DIR / "router.log"
WATCH_LOG = LLM_DIR / "sol-llm.log"
TAIL_BYTES = 256 * 1024
MIN_TOKENS = 64              # very short answers give noisy speeds
SLOW_FRACTION = 0.5          # below half the usual speed: say so
# Typical generation speed with room on the card. sol-fast's MTP draft guesses code far better than prose: the A/B on
# 2026-10-07 (reports\ai-v2-bench.md, ab-*) gave 127-138 tok/s on code and the same model 73 tok/s on a story, so 100
# is the middle and "slow" (under half) is under 50. With the card crowded (12.3 GB in use) the story ran at 26.
USUAL_TPS = {"sol-fast": 100, "sol-vision": 100, "sol-smart": 64, "sol-long": 60}
HEAL_RECENT_S = 30 * 60      # a heal line older than this is history, not status

_EVAL = re.compile(r"^\[(\d+)\].*?(?:task\s+(\d+)\s*)?\|\s+eval time =\s+([\d.]+) ms /\s+(\d+) tokens.*?([\d.]+) tokens per second")
_HEAL = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) .*?Auto-heal (waiting|trigger): (.*)$")


def _tail(path: Path, nbytes: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_speeds(text: str) -> list[dict]:
    """Answer timings from llama.cpp's log, oldest first. Each line starts with [port] of the model process that
    wrote it. `prompt eval time` lines are reading the prompt, not answering."""
    out = []
    for line in text.splitlines():
        if "prompt eval time" in line:
            continue
        m = _EVAL.search(line)
        if m and int(m.group(4)) >= MIN_TOKENS:
            out.append({"port": int(m.group(1)), "task": int(m.group(2)) if m.group(2) else None, "ms": float(m.group(3)),
                        "tokens": int(m.group(4)), "tps": round(float(m.group(5)), 1)})
    return out


def answer_speed(procs: dict | None = None, path: Path = ROUTER_LOG) -> dict:
    """The last answer's speed, which model gave it (by the port of its process), and whether that's slow for it."""
    speeds = parse_speeds(_tail(path))
    if not speeds:
        return {"available": False}
    by_port = {int(v["port"]): alias for alias, v in (procs or {}).items() if isinstance(v, dict) and v.get("port")}
    last = speeds[-1]
    model = by_port.get(last["port"])
    same = [s["tps"] for s in speeds if s["port"] == last["port"]][-5:]
    usual = USUAL_TPS.get(model or "")
    try:
        at = path.stat().st_mtime
    except OSError:
        at = None
    return {"available": True, "model": model, "tps": last["tps"], "tokens": last["tokens"], "at": at,
            "answer_id": (last["port"], last.get("task")),
            "recent": same, "usual": usual, "slow": bool(usual and last["tps"] < usual * SLOW_FRACTION),
            "current_process": model is not None}


def heal_status(path: Path = WATCH_LOG, now: float | None = None) -> dict:
    """The watcher's latest auto-heal line, if recent: waiting (and why) or reloaded."""
    now = time.time() if now is None else now
    for line in reversed(_tail(path, 64 * 1024).splitlines()):
        m = _HEAL.search(line)
        if not m:
            continue
        try:
            at = datetime.fromisoformat(m.group(1)).timestamp()
        except ValueError:
            continue
        if now - at > HEAL_RECENT_S:
            return {"available": True, "recent": False}
        return {"available": True, "recent": True, "kind": m.group(2), "text": m.group(3).strip(), "at": at}
    return {"available": True, "recent": False}
