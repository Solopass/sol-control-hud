from sol_control_hud.doctor import FAIL, OK, WARN, checks

HEALTHY = {
    "ollama": {"up": True, "version": "0.34.0", "loaded": [{"name": "gpt-oss:20b", "gpu_percent": 100, "context": 8192}]},
    "backend": {"known": True, "time": "2026-09-14T09:56:57", "library": "Vulkan", "device": "AMD Radeon RX 9070 XT"},
    "llama_swap": {"up": False},
    "gpu": {"available": True, "name": "RX 9070 XT", "load_percent": 3.0, "vram_used_gb": 12.0, "vram_total_gb": 15.9},
    "memory": {"total_gb": 95.7, "used_gb": 20.0, "percent": 20.9},
    "disks": [{"drive": "C", "free_gb": 800, "total_gb": 930, "percent": 14}],
    "backups": {"latest": "ai-dev-backup-2026-09-13.tar", "age_hours": 3.0, "stale": False},
    "wsl": {"available": True, "distro": "Ubuntu-24.04", "state": "Stopped"},
    "stability": {"available": True, "whea": 0, "unexpected_reboots": 0, "gpu_driver_resets": 0},
}


def levels(status):
    return {name.strip(): level for name, level, _ in checks(status)}


def test_healthy_machine_is_all_ok():
    assert set(levels(HEALTHY).values()) == {OK}


def test_engine_down_and_hardware_errors_fail():
    bad = {**HEALTHY, "ollama": {"up": False}, "stability": {**HEALTHY["stability"], "whea": 2}}
    lv = levels(bad)
    assert lv["AI engine (Ollama)"] == FAIL and lv["Stability since baseline"] == FAIL


def test_spilled_model_and_stale_backup_warn():
    s = {**HEALTHY,
         "ollama": {"up": True, "version": "x", "loaded": [{"name": "gpt-oss:120b", "gpu_percent": 22, "context": 8192}]},
         "backups": {"latest": "old.tar", "age_hours": 400, "stale": True}}
    lv = levels(s)
    assert lv["model gpt-oss:120b"] == WARN and lv["WSL backup"] == WARN


def test_wrong_backend_fails_and_unknown_warns():
    assert levels({**HEALTHY, "backend": {**HEALTHY["backend"], "library": "ROCm"}})["GPU backend"] == FAIL
    assert levels({**HEALTHY, "backend": {"known": False}})["GPU backend"] == WARN


def test_backend_parser_picks_newest_line_across_logs(tmp_path):
    from sol_control_hud.data.collectors.engines import ollama_backend
    line = 'time={t} level=INFO source=types.go:32 msg="inference compute" id=0 library={lib} compute=0.0 name=X description="AMD Radeon RX 9070 XT" libdirs=ollama\n'
    (tmp_path / "server-2.log").write_text(line.format(t="2026-09-13T22:14:30.339-04:00", lib="ROCm"))
    (tmp_path / "server.log").write_text("noise\n" + line.format(t="2026-09-14T09:56:57.121-04:00", lib="Vulkan"))
    (tmp_path / "app.log").write_text(line.format(t="2026-09-15T00:00:00.000-04:00", lib="CPU"))  # not a server log
    be = ollama_backend(tmp_path)
    assert be["library"] == "Vulkan" and be["log"] == "server.log"
    assert ollama_backend(tmp_path / "missing") == {"known": False}


def test_acknowledged_crash_moves_the_stability_window(tmp_path, monkeypatch):
    from datetime import datetime
    from sol_control_hud.data.collectors import system
    ack = tmp_path / "ack.json"
    ack.write_text('{"acknowledged_until": "2026-09-14T03:40:00", "note": "analyzed"}')
    assert system.acknowledged_until(ack) == (datetime(2026, 9, 14, 3, 40), "analyzed")
    assert system.acknowledged_until(tmp_path / "none.json") == (None, "")
    seen = {}
    monkeypatch.setattr(system, "acknowledged_until", lambda: (datetime(2026, 9, 14, 3, 40), "analyzed"))
    monkeypatch.setattr(system, "_run", lambda args, timeout: seen.setdefault("ps", args[-1]) and "0 0 0")
    s = system.stability()
    assert "2026-09-14 03:40:00" in seen["ps"] and s["acknowledged"] == "analyzed" and s["unexpected_reboots"] == 0
    s = {**HEALTHY, "stability": {"available": True, "since": "2026-09-14T03:40:00", "acknowledged": "analyzed",
                                  "whea": 0, "unexpected_reboots": 1, "gpu_driver_resets": 0}}
    assert levels(s)["Stability since baseline"] == FAIL  # a NEW event after the ack still fails


def test_failed_units_warn_when_wsl_running():
    s = {**HEALTHY, "wsl": {"available": True, "distro": "Ubuntu-24.04", "state": "Running", "failed_units": ["media-api.service"]}}
    assert levels(s)["WSL Ubuntu-24.04"] == WARN
