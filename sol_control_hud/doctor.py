"""sol-doctor: honest one-shot health check built on the HUD's collectors. Exit 0 = all OK, 1 = warnings, 2 = failures.

    .venv\\Scripts\\python.exe -m sol_control_hud.doctor [--json]
"""
from __future__ import annotations

import json
import sys
import time

from .data.collectors import engines, gpu, system, vram

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def checks(status: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    o = status["ollama"]
    out.append(("AI engine (Ollama)", OK if o.get("up") else FAIL, f"v{o.get('version')}, {len(o.get('loaded') or [])} model(s) loaded" if o.get("up") else "not responding on 127.0.0.1:11434"))
    v = status.get("vram") or {}
    for m in o.get("loaded") or []:
        pct = m.get("gpu_percent")
        if v.get("available") and v.get("evicted"):
            out.append((f"  model {m['name']}", WARN, f"{pct}% reported by Ollama, but {v['ai']['shared_gb']} GB is evicted to system RAM"))
        else:
            out.append((f"  model {m['name']}", OK if (pct or 0) >= 60 else WARN, f"{pct}% on GPU, ctx {m.get('context')}"))
    be = status.get("backend") or {}
    if not be.get("known"):
        out.append(("  GPU backend", WARN, "no 'inference compute' line in Ollama server logs"))
    else:
        out.append(("  GPU backend", OK if be["library"] == "Vulkan" else FAIL,
                    f"library={be['library']} ({be['device']}) at {be['time'][:16]}" + ("" if be["library"] == "Vulkan" else " - expected Vulkan; run restart-ollama.ps1")))

    g = status["gpu"]
    if g.get("available"):
        total = g.get("vram_total_gb") or 16
        out.append(("GPU", OK, f"{g.get('name')}: load {g.get('load_percent')}%, VRAM {g.get('vram_used_gb')}/{total} GB"))
    else:
        out.append(("GPU", WARN, f"counters unavailable ({g.get('error', 'unknown')})"))

    v = status.get("vram") or {}
    if v.get("available"):
        free = ", ".join(f"{s['label']} {s['gb']} GB" for s in v.get("suggest_free") or []) or "nothing obvious"
        if v["evicted"]:
            out.append(("  Model eviction", FAIL, f"{v['ai']['shared_gb']} GB of the model is in system RAM (expect 2-5x slower). "
                                                  f"Free VRAM ({free}), then reload the model"))
        elif v["ai"]["dedicated_gb"] > 0.5:
            out.append(("  Model eviction", OK, f"model fully on the card ({v['ai']['dedicated_gb']} GB)"))
        verdict, spare = v["verdict"], v.get("spare_gb")
        level = {"OK": OK, "FITS": OK, "TIGHT": WARN, "WONT_FIT": WARN, "EVICTED": WARN}.get(verdict, WARN)
        detail = {"WONT_FIT": f"{v['need']['model']} needs ~{v['need']['gb']} GB, only {v['room_gb']} GB free of other programs; free: {free}",
                  "TIGHT": f"{spare} GB spare; more browser tabs or video may push the model out",
                  "UNKNOWN": f"no size known for {v['need']['model']}"}.get(verdict, f"{spare} GB spare, other programs use {v['others_gb']} GB")
        out.append(("  VRAM headroom", level, detail))

    m = status["memory"]
    out.append(("RAM", OK if m["percent"] < 90 else WARN, f"{m['used_gb']}/{m['total_gb']} GB ({m['percent']}%)"))

    for d in status["disks"]:
        if "error" in d:
            out.append((f"Disk {d['drive']}:", FAIL, "unavailable"))
        else:
            free_level = FAIL if d["free_gb"] < 20 else WARN if d["percent"] > 90 else OK
            out.append((f"Disk {d['drive']}:", free_level, f"{d['free_gb']} GB free ({d['percent']}% used)"))

    b = status["backups"]
    if not b.get("latest"):
        out.append(("WSL backup", FAIL, "no ai-dev-backup-*.tar on E:"))
    else:
        out.append(("WSL backup", WARN if b.get("stale") else OK, f"{b['latest']} ({b['age_hours']} h old)"))

    w = status["wsl"]
    if not w.get("available"):
        out.append(("WSL", WARN, "wsl.exe not responding"))
    else:
        failed = w.get("failed_units")
        detail = w["state"] if w["state"] != "Running" else f"Running, failed units: {len(failed) if failed is not None else '?'}"
        level = FAIL if w["state"] == "not registered" else WARN if failed else OK
        out.append(("WSL Ubuntu-24.04", level, detail + (f" ({', '.join(failed)})" if failed else "")))

    s = status["stability"]
    if s.get("available"):
        bad = s["whea"] + s["unexpected_reboots"] + s["gpu_driver_resets"]
        since = f" since {s['since'][:16]} (earlier events acknowledged)" if s.get("acknowledged") else ""
        out.append(("Stability since baseline", FAIL if bad else OK,
                    f"WHEA {s['whea']}, unexpected reboots {s['unexpected_reboots']}, GPU driver resets {s['gpu_driver_resets']}{since}"
                    + (" - run D:\\OBVLT\\tools\\crash-watch.ps1" if bad else "")))
    else:
        out.append(("Stability since baseline", WARN, "event log query failed"))
    return out


def collect() -> dict:
    sampler = gpu.GpuSampler(interval=1.0)
    sampler.start()
    time.sleep(2.5)  # two counter samples for a real utilization value
    ollama = engines.ollama()
    status = {
        "ollama": ollama, "backend": engines.ollama_backend(), "llama_swap": engines.llama_swap(), "gpu": sampler.latest,
        "vram": vram.VramGuard(confirm=1).update(sampler.latest, ollama),
        "memory": system.memory(), "disks": system.disks(), "backups": system.backups(),
        "wsl": system.wsl(), "stability": system.stability(),
    }
    sampler.stop()
    return status


def main(argv: list[str]) -> int:
    results = checks(collect())
    worst = 2 if any(r[1] == FAIL for r in results) else 1 if any(r[1] == WARN for r in results) else 0
    if "--json" in argv:
        print(json.dumps({"exit": worst, "checks": [{"name": n, "level": l, "detail": d} for n, l, d in results]}, indent=2))
    else:
        for name, level, detail in results:
            print(f"[{level:4}] {name:28} {detail}")
        print({0: "all OK", 1: "warnings", 2: "FAILURES"}[worst])
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
