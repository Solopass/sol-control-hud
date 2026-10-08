"""sol-doctor: honest one-shot health check built on the HUD's collectors. Exit 0 = all OK, 1 = warnings, 2 = failures.

    .venv\\Scripts\\python.exe -m sol_control_hud.doctor [--json]
"""
from __future__ import annotations

import json
import sys
import time

from .data.collectors import displays, engines, gpu, speed, system, vram

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def checks(status: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    ls = status.get("llama_swap") or {}
    o = status.get("ollama") or {}
    router_up = bool(ls.get("up"))
    shim_up = bool(o.get("up"))
    if router_up:
        out.append(("AI engine (llama.cpp router)", OK,
                    f":11440 up ({len(ls.get('running') or [])} model(s) served)" + (f", shim :11434 v{o.get('version')}" if shim_up else ", shim :11434 down")))
    elif shim_up:
        out.append(("AI engine (Ollama shim)", WARN, f":11434 up (v{o.get('version')}), but router :11440 not responding"))
    else:
        out.append(("AI engine", FAIL, "neither router :11440 nor shim :11434 responding"))

    v = status.get("vram") or {}
    for m in o.get("loaded") or []:
        pct = m.get("gpu_percent")
        if pct is None and v.get("ai", {}).get("dedicated_gb", 0) > 0.5 and not v.get("evicted"):
            pct = 100
        ctx_str = f", ctx {m.get('context')}" if m.get("context") else ""
        if v.get("available") and v.get("evicted"):
            out.append((f"  model {m['name']}", WARN, f"{pct or 0}% reported by Ollama, but {v['ai']['shared_gb']} GB is evicted to system RAM"))
        else:
            out.append((f"  model {m['name']}", OK if (pct or 0) >= 60 else WARN, f"{pct or 0}% on GPU{ctx_str}"))

    be = status.get("backend") or {}
    if be.get("known") is False:
        out.append(("  GPU backend", WARN, "unknown (no backend logs found)"))
    elif be.get("known") and be.get("library"):
        lib = be.get("library")
        out.append(("  GPU backend", OK if lib == "Vulkan" else FAIL,
                    f"library={lib} ({be.get('device', 'AMD Radeon RX 9070 XT')})" + ("" if lib == "Vulkan" else " - expected Vulkan; check sol-llm.ps1")))
    elif router_up:
        out.append(("  GPU backend", OK, "Vulkan (llama.cpp router on AMD Radeon RX 9070 XT)"))
    else:
        out.append(("  GPU backend", WARN, "no GPU backend information available"))

    sp = status.get("speed") or {}
    if sp.get("available"):
        slow = sp.get("slow", False)
        out.append(("  Answer speed", WARN if slow else OK,
                    f"{sp.get('model') or 'last model'}: {sp.get('tps')} tok/s (usual ~{sp.get('usual') or 100})" + (" - SLOW!" if slow else "")))

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

    d = status.get("displays") or {}
    if d.get("available"):
        dark, busy, pinned = d.get("dark") or [], d.get("busy") or [], d.get("power_saving_apps") or []
        if busy:
            who = ", ".join(f"{r['name']} {r['percent']}%" for r in busy[:4])
            out.append(("  Rendering on the wrong GPU", WARN,
                        f"{who} rendering on {busy[0]['adapter']}, which has no monitor: every frame is copied to the card "
                        f"that does. Windows Settings > Display > Graphics -> 'Let Windows decide'"))
        elif d.get("setting_problem"):
            out.append(("  Rendering on the wrong GPU", WARN,
                        f"{len(pinned)} app(s) set to power saving ({', '.join(pinned[:4])}) while {dark[0]} has no "
                        f"monitor: they will render there and be copied across"))
        elif dark:
            out.append(("  Rendering on the wrong GPU", OK, f"{dark[0]} drives no monitor, and nothing is rendering on it"))

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
        "displays": displays.report(sampler.last_util, lambda pid: gpu.process_name(pid, {})),
        "speed": speed.answer_speed(),
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
