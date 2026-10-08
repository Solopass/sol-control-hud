"""Model controls for the dashboard: what each model is doing, where its memory is, and load / unload / all off.

- States come from the router (GET :11440/models: it answers itself; never /slots, that wakes a model and keeps it).
- Memory per model: the router runs each loaded model as its own llama-server process tagged `--alias <name>`, so
  the GPU counters (dedicated = in VRAM, shared = spilled into system RAM) and the process's RAM can be matched to it.
- Load / unload go to the router (POST /models/load|unload); in Desk mode it holds one model at a time, so loading one
  swaps out the other. While Away runs they're refused: the Away job owns the GPU (stop it first).
- AI on / off = tools\\sol-llm.ps1 desk / off (off: nothing loads until you turn it on again).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import psutil

from .data.collectors.engines import LLAMA_SWAP, _client
from .swallow import note as _swallowed

MODELS_JSON = Path(os.environ.get("SOL_MODELS", r"D:\OBVLT\tools\models.json"))
LOCAL_AI = Path(os.environ.get("SOL_LOCAL_AI", r"D:\OBVLT\configs\local-ai.json"))
SOL_LLM = Path(r"D:\OBVLT\tools\sol-llm.ps1")


class ModelError(ValueError):
    pass


def descriptions() -> dict[str, str]:
    """name -> what it is (models.json 'base' for Desk models, the Away model files from local-ai.json)."""
    out: dict[str, str] = {}
    try:
        for name, m in json.loads(MODELS_JSON.read_text(encoding="utf-8-sig")).get("models", {}).items():
            if isinstance(m, dict) and m.get("base"):
                out[name] = str(m["base"])
    except (OSError, ValueError):
        pass
    try:
        away = (json.loads(LOCAL_AI.read_text(encoding="utf-8-sig")).get("away") or {}).get("models", {})
        for name, m in away.items():
            if isinstance(m, dict) and m.get("file"):
                out[name] = str(m["file"]).removesuffix(".gguf")
    except (OSError, ValueError):
        pass
    return out


def model_processes() -> dict[str, dict]:
    """alias -> {pid, port, ram_gb} for the router's model processes (and the embedder)."""
    out = {}
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() != "llama-server.exe":
            continue
        try:
            cmd = p.cmdline()
            if "--alias" not in cmd:
                continue
            alias = cmd[cmd.index("--alias") + 1].split(",")[0]
            port = int(cmd[cmd.index("--port") + 1]) if "--port" in cmd else None   # names its lines in router.log
            out[alias] = {"pid": p.pid, "port": port, "ram_gb": round(p.memory_info().rss / 1024**3, 2)}
        except (psutil.Error, IndexError, ValueError):
            continue
    return out


def rows(router: dict, gpu: dict, procs: dict[str, dict], desc: dict[str, str]) -> list[dict]:
    """One row per model the router serves: state, what it is, VRAM / spilled / RAM when it's running."""
    by_pid = {p.get("pid"): p for p in (gpu or {}).get("processes", []) or []}
    out = []
    for m in (router or {}).get("running") or []:
        name = m.get("model")
        if not name:
            continue
        proc = procs.get(name) or {}
        g = by_pid.get(proc.get("pid"), {})
        out.append({"id": name, "state": m.get("state") or "unloaded", "about": desc.get(name, ""),
                    "vram_gb": g.get("dedicated_gb"), "spilled_gb": g.get("shared_gb"), "ram_gb": proc.get("ram_gb")})
    if "sol-embed" in procs:        # the embedder: its own small server on :11443, always on (not the router's)
        p = procs["sol-embed"]
        out.append({"id": "sol-embed", "state": "loaded", "about": desc.get("sol-embed", "embeddings (CPU)"),
                    "vram_gb": by_pid.get(p["pid"], {}).get("dedicated_gb"), "spilled_gb": None, "ram_gb": p["ram_gb"],
                    "fixed": True})
    return out


def _mode() -> str:
    try:
        return json.loads(Path(r"D:\AI\Cache\llm\state.json").read_text(encoding="utf-8-sig")).get("mode", "desk")
    except (OSError, ValueError):
        return "desk"


def model_op(name: str, op: str, served: list[str], mode: str | None = None, post=None) -> str:
    """load / unload one model (in the background: loading a big one takes a while). Returns what's happening."""
    mode = mode or _mode()
    if mode == "away":
        raise ModelError("Away is running: its job owns the GPU (Stop AI work first)")
    if mode == "off":
        raise ModelError("the local AI is off: turn it on first")
    if op not in ("load", "unload"):
        raise ModelError(f"unknown action {op!r}")
    if name not in served:
        raise ModelError(f"{name} isn't one of the router's models")
    post = post or (lambda path, body: _client().post(f"{LLAMA_SWAP}{path}", json=body, timeout=300))
    threading.Thread(target=lambda: _quiet(post, f"/models/{op}", {"model": name}), name=f"model-{op}", daemon=True).start()
    return f"{'loading' if op == 'load' else 'unloading'} {name}…"


def unload_all(loaded: list[str], mode: str | None = None, post=None) -> str:
    mode = mode or _mode()
    if mode == "away":
        raise ModelError("Away is running: its job owns the GPU (Stop AI work first)")
    post = post or (lambda path, body: _client().post(f"{LLAMA_SWAP}{path}", json=body, timeout=120))
    for name in loaded:
        threading.Thread(target=lambda n=name: _quiet(post, "/models/unload", {"model": n}), daemon=True).start()
    return f"unloading {', '.join(loaded)}…" if loaded else "nothing is loaded"


def ai_power(on: bool, run=None) -> str:
    """AI on = Desk mode (models load when asked), off = nothing loads until you turn it on (sol-llm.ps1)."""
    if not on and _mode() == "away":
        raise ModelError("Away is running: use Stop AI work")
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SOL_LLM), "desk" if on else "off"]
    if not on:
        args += ["-Reason", "turned off on the dashboard"]
    (run or (lambda a: subprocess.Popen(a, creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))(args)
    return "turning the local AI on (Desk)…" if on else "turning the local AI off (everything unloads)…"


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001 - the next status update shows what really happened
        _swallowed("models_ctl._quiet")
