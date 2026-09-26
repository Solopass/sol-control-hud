"""SOL HUD v2 - M1: read-only status at a glance. Bind to 127.0.0.1 only."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ...data.collectors import engines, gpu, system, vram
from ...paths import DATA_DIR

STATIC = Path(__file__).parent / "static"


class Cached:
    """Runs a collector at most once per `ttl` seconds; a failing collector reports an error instead of breaking /api/status."""

    def __init__(self, fn: Callable[[], object], ttl: float):
        self.fn, self.ttl = fn, ttl
        self.value: object = None
        self.at = 0.0

    def get(self) -> object:
        if self.value is None or time.monotonic() - self.at >= self.ttl:
            try:
                self.value = self.fn()
            except Exception as e:  # noqa: BLE001 - one broken collector must not take the page down
                self.value = {"available": False, "error": f"{type(e).__name__}: {e}"}
            self.at = time.monotonic()
        return self.value


def build_collectors(sampler: gpu.GpuSampler, guard: vram.GuardLoop) -> dict[str, Cached]:
    return {
        "ollama": Cached(engines.ollama, 2),
        "llama_swap": Cached(engines.llama_swap, 2),
        "gpu": Cached(lambda: sampler.latest, 0),
        "vram": Cached(lambda: guard.latest, 0),
        "memory": Cached(system.memory, 2),
        "disks": Cached(system.disks, 30),
        "backups": Cached(system.backups, 60),
        "wsl": Cached(system.wsl, 10),
        "stability": Cached(system.stability, 60),
    }


def create_app(collectors: dict[str, Cached] | None = None, sampler: gpu.GpuSampler | None = None) -> FastAPI:
    background: list = []
    if collectors is None:
        sampler = sampler or gpu.GpuSampler()
        guard = vram.GuardLoop(vram.VramGuard(), sampler, engines.ollama)
        collectors = build_collectors(sampler, guard)
        background = [sampler, guard]
    elif sampler:
        background = [sampler]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for b in background:
            b.start()
        yield
        for b in background:
            b.stop()

    app = FastAPI(title="SOL HUD", lifespan=lifespan)
    app.state.collectors = collectors

    @app.get("/api/status")
    def status() -> dict:
        data = {name: c.get() for name, c in app.state.collectors.items()}
        data["generated_at"] = time.time()
        return data

    @app.get("/")
    def index() -> FileResponse:
        # no-store: after a HUD update the browser must not keep running the old page (09-25: a fixed bug stayed "broken")
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    return app


def main() -> None:
    import sys

    import uvicorn

    if sys.stdout is None or sys.stderr is None:
        # started without a console (pythonw, the "SOL HUD" desktop shortcut): uvicorn's logging would crash on None
        log = open(DATA_DIR / "hud.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    sampler = gpu.GpuSampler()
    uvicorn.run(create_app(sampler=sampler), host="127.0.0.1", port=7900, log_level="warning")


if __name__ == "__main__":
    main()
