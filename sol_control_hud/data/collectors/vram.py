"""VRAM guard: does the AI model fit next to everything else on the card, and has Windows evicted part of it?

Ollama's /api/ps reports its own allocation ("100% GPU") even when Windows has moved part of the model to system RAM
because other programs grew. Per-process Shared Usage on the AI runner is the real signal
(2026-09-16: llama-server 10.47 GB dedicated + 1.76 GB shared -> ~23 tok/s instead of ~125).
See D:\\OBVLT\\plans\\LOCAL_AI_SPEED_FIX.md and HUD_VRAM_GUARD_PLAN.md."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from ...paths import DATA_DIR

RUNNER = re.compile(r"^(llama-server|ollama)$", re.IGNORECASE)
EVICTED_SHARED_GB = 0.3      # runner memory in system RAM above this = evicted
HEADROOM_GB = 0.8            # spare VRAM below this = TIGHT
DEFAULT_MODEL = "sol-fast"
LABELS = {"dwm": "Windows desktop (monitors)", "RadeonSoftware": "AMD Adrenalin", "explorer": "Explorer"}
NOT_MOVABLE = {"dwm", "explorer", "csrss", "RadeonSoftware", "AMDRSServ", "AMDRSSrcExt"}

# (num_ctx, GB on the card) until a real load is observed. sol-coder / sol-specialist split with the CPU by design.
SEED_NEEDS = {
    # Local AI v2 (2026-09-25, llama.cpp router; OBVLT reports\ai-v2-bench.md). The Ollama-era values are in git history.
    "sol-fast": (32768, 7.9),      # Gemma 4 12B QAT + MTP, fully on the card
    "sol-vision": (32768, 7.9),    # same process as sol-fast
    "sol-smart": (32768, 9.4),     # gpt-oss-20b, experts past the margin go to RAM on purpose
    "sol-specialist": (32768, 12.5),  # Away: Qwen3.8-27B GSQ + MTP, whole card
    "sol-coder": (32768, 11.0),    # Away: MoE with experts in RAM (fit-target)
}
NEEDS_FILE = DATA_DIR / "vram-needs.json"


def _alias(name: str) -> str:
    return (name or "").removesuffix(":latest")


class NeedStore:
    """GB each model takes on the card, keyed by (alias, num_ctx). Learned from real loads that weren't evicted."""

    def __init__(self, path: Path | None = NEEDS_FILE):
        self.path = path
        self.learned: dict[str, float] = {}
        if path and path.exists():
            try:
                self.learned = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.learned = {}

    def get(self, alias: str, ctx: int | None = None) -> tuple[float | None, int | None, str]:
        seed_ctx, seed_gb = SEED_NEEDS.get(alias, (None, None))
        ctx = ctx or seed_ctx
        key = f"{alias}@{ctx}"
        if key in self.learned:
            return self.learned[key], ctx, "learned"
        return seed_gb, ctx, "estimate"

    def learn(self, alias: str, ctx: int, gb: float) -> None:
        key = f"{alias}@{ctx}"
        if abs(self.learned.get(key, -99) - gb) < 0.1:
            return
        self.learned[key] = round(gb, 2)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # write beside it and rename: a crash or power loss mid-write would otherwise leave half a JSON file
                # and the learned model sizes would be gone (found by the Code review chain, 2026-09-29)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(self.learned, indent=1, sort_keys=True), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


class GuardLoop:
    """Runs the guard every `interval` seconds in its own thread, so eviction is detected (and sizes learned) even
    when no browser has the HUD open. Owns its Ollama polling so a slow or down engine never stalls the GPU sampler."""

    def __init__(self, guard: "VramGuard", sampler, ollama_fn, interval: float = 2.0):
        import threading
        self.guard, self.sampler, self.ollama_fn, self.interval = guard, sampler, ollama_fn, interval
        self.latest: dict = {"available": False, "error": "starting"}
        self._stop = threading.Event()
        self._last_loaded: tuple[str, ...] | None = None
        self._thread = threading.Thread(target=self._run, name="vram-guard", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                ollama = self.ollama_fn()
                current_loaded = tuple(sorted(m.get("name", "") for m in (ollama.get("loaded") or []))) if ollama.get("up") else ()
                if self._last_loaded is not None and current_loaded != self._last_loaded:
                    if hasattr(self.sampler, "request_rescan"):
                        self.sampler.request_rescan()
                self._last_loaded = current_loaded
                self.latest = self.guard.update(self.sampler.latest, ollama)
            except Exception as e:  # noqa: BLE001 - keep the loop alive; the error shows on the card
                self.latest = {"available": False, "error": f"{type(e).__name__}: {e}"}


class VramGuard:
    """Stateful only for hysteresis: evicted must be seen on two consecutive samples to set, and to clear."""

    def __init__(self, needs: NeedStore | None = None, default_model: str = DEFAULT_MODEL, confirm: int = 2):
        self.needs = needs if needs is not None else NeedStore()
        self.default_model = default_model
        self.confirm = confirm      # consecutive distinct samples needed to flip; 1 for one-shot checks (sol-doctor)
        self._evicted = False
        self._streak = 0
        self._last_sample = None

    def update(self, gpu: dict, ollama: dict) -> dict:
        if not gpu.get("available") or "processes" not in gpu:
            return {"available": False, "error": gpu.get("error") or "GPU counters unavailable"}
        card = gpu.get("vram_total_gb") or 16.0
        procs = gpu["processes"]
        runner = [p for p in procs if RUNNER.match(p["name"] or "")]
        ai_ded = sum(p["dedicated_gb"] for p in runner)
        ai_shr = sum(p["shared_gb"] for p in runner)

        # Per-process "Dedicated Usage" double-counts memory shared between processes (09-16: processes summed to 18.95 GB
        # on a card holding 14.86 GB). The adapter total is the truth; per-process numbers only apportion it.
        used = gpu.get("vram_used_gb")
        others_raw: dict[str, float] = {}
        for p in procs:
            if p in runner:
                continue
            others_raw[p["name"]] = others_raw.get(p["name"], 0.0) + p["dedicated_gb"]
        raw_sum = sum(others_raw.values())
        others_gb = max(0.0, used - ai_ded) if used is not None else raw_sum
        scale = others_gb / raw_sum if raw_sum > 0 else 0.0
        top = [{"name": n, "label": LABELS.get(n, n), "gb": round(gb * scale, 2), "movable": n not in NOT_MOVABLE}
               for n, gb in sorted(others_raw.items(), key=lambda kv: kv[1], reverse=True) if gb * scale >= 0.05]

        loaded = [m for m in (ollama.get("loaded") or [])] if ollama.get("up") else []
        raw = ai_shr > EVICTED_SHARED_GB and ai_ded > 0.5
        sample = gpu.get("sampled_at")
        if sample is None or sample != self._last_sample:   # only a new sample counts toward the streak
            self._last_sample = sample
            if raw == self._evicted:
                self._streak = 0
            else:
                self._streak += 1
                if self._streak >= self.confirm:
                    self._evicted, self._streak = raw, 0
        evicted = self._evicted

        if len(loaded) == 1 and not raw and ai_ded > 0.5 and loaded[0].get("context"):
            self.needs.learn(_alias(loaded[0]["name"]), int(loaded[0]["context"]), ai_ded)

        room = card - others_gb
        need_gb, need_ctx, need_src = self.needs.get(self.default_model)
        if ai_ded > 0.5:
            spare = card - others_gb - ai_ded
            verdict = "EVICTED" if evicted else ("OK" if spare >= HEADROOM_GB else "TIGHT")
        else:
            spare = None if need_gb is None else room - need_gb
            verdict = "UNKNOWN" if spare is None else "FITS" if spare >= HEADROOM_GB else "TIGHT" if spare >= 0 else "WONT_FIT"

        return {
            "available": True,
            "card_gb": card,
            "others_gb": round(others_gb, 2),
            "room_gb": round(room, 2),
            "ai": {"processes": [p["name"] for p in runner], "dedicated_gb": round(ai_ded, 2), "shared_gb": round(ai_shr, 2),
                   "models": [m.get("name") for m in loaded]},
            "evicted": evicted,
            "verdict": verdict,
            "spare_gb": None if spare is None else round(spare, 2),
            "need": {"model": self.default_model, "gb": need_gb, "ctx": need_ctx, "source": need_src},
            "used_gb": used,
            "top_consumers": top[:8],       # apportioned from per-process counters, approximate
            "suggest_free": [t for t in top if t["movable"] and t["gb"] >= 0.2][:4],
        }
