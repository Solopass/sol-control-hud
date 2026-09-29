"""AI engine state: Windows Ollama (11434) and llama-swap (11440). Read-only."""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx

OLLAMA = "http://127.0.0.1:11434"
LLAMA_SWAP = "http://127.0.0.1:11440"
OLLAMA_LOGS = Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama"
_HTTP: httpx.Client | None = None


def _client() -> httpx.Client:
    """One client for the process: building one per call cost ~5-10 ms CPU each (twice every 2 s with the dashboard open)."""
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client()
    return _HTTP


_COMPUTE = re.compile(r'time=(\S+) .*msg="inference compute".*?library=(\S+).*?description="([^"]*)"')


def ollama_backend(log_dir: Path = OLLAMA_LOGS) -> dict:
    """Which GPU backend the running Ollama picked, from the newest 'inference compute' line in its server logs (I17).
    Vulkan is the chosen backend; ROCm or no GPU line means the engine started wrong."""
    newest = None
    for log in log_dir.glob("server*.log"):
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _COMPUTE.finditer(text):
            if newest is None or m.group(1) > newest[0]:
                newest = (m.group(1), m.group(2), m.group(3), log.name)
    if newest is None:
        return {"known": False}
    return {"known": True, "time": newest[0], "library": newest[1], "device": newest[2], "log": newest[3]}


def ollama(timeout: float = 1.5) -> dict:
    try:
        c = _client()
        version = c.get(f"{OLLAMA}/api/version", timeout=timeout).json().get("version")
        loaded = c.get(f"{OLLAMA}/api/ps", timeout=timeout).json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return {"up": False}
    models = []
    for m in loaded:
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        models.append({
            "name": m.get("name"),
            "size_gb": round(size / 1024**3, 1),
            "vram_gb": round(vram / 1024**3, 1),
            "gpu_percent": round(100 * vram / size) if size else None,
            "context": m.get("context_length"),
            "expires_at": m.get("expires_at"),
        })
    return {"up": True, "version": version, "loaded": models}


def llama_swap(timeout: float = 1.5) -> dict:
    """What runs on :11440. Since 2026-09-25 that is the llama.cpp router (Local AI v2, D:\\OBVLT\\tools\\sol-llm.ps1),
    which lists models at /models ({"data": [{"id", "status": {"value": "loaded"|...}}]}); llama-swap used /running.
    Both are understood; anything else is reported as up with no models instead of crashing."""
    try:
        c = _client()
        router = c.get(f"{LLAMA_SWAP}/models", timeout=timeout)   # an answer here already means it's up
        body = router.json() if router.status_code == 200 else None
        if isinstance(body, dict) and "data" in body:
            return {"up": True, "engine": "llama.cpp router",
                    "running": [{"model": m.get("id"), "state": (m.get("status") or {}).get("value")}
                                for m in body["data"] if isinstance(m, dict)]}
        if c.get(f"{LLAMA_SWAP}/health", timeout=timeout).status_code != 200:
            return {"up": False}
        running = c.get(f"{LLAMA_SWAP}/running", timeout=timeout).json()
    except (httpx.HTTPError, ValueError):
        return {"up": False}
    items = running.get("running", running) if isinstance(running, dict) else running
    return {"up": True, "engine": "llama-swap",
            "running": [{"model": r.get("model"), "state": r.get("state")} for r in (items or []) if isinstance(r, dict)]}
