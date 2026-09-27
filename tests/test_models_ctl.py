"""Model controls on the dashboard: rows (state + where the memory is) and the rules for load / unload / AI off."""
import time

import pytest

from sol_control_hud import models_ctl as mc

ROUTER = {"up": True, "running": [{"model": "sol-fast", "state": "loaded"}, {"model": "sol-smart", "state": "unloaded"}]}
GPU = {"processes": [{"pid": 11, "name": "llama-server", "dedicated_gb": 7.13, "shared_gb": 1.1},
                     {"pid": 99, "name": "brave", "dedicated_gb": 0.6, "shared_gb": 0.0}]}
PROCS = {"sol-fast": {"pid": 11, "ram_gb": 7.82}, "sol-embed": {"pid": 12, "ram_gb": 0.15}}


def test_rows_match_memory_to_each_model():
    rows = mc.rows(ROUTER, GPU, PROCS, {"sol-fast": "gemma-4-12B", "sol-embed": "nomic (CPU)"})
    assert rows[0] == {"id": "sol-fast", "state": "loaded", "about": "gemma-4-12B", "vram_gb": 7.13, "spilled_gb": 1.1,
                       "ram_gb": 7.82}
    assert rows[1]["id"] == "sol-smart" and rows[1]["vram_gb"] is None                  # not running: no memory
    assert rows[2]["id"] == "sol-embed" and rows[2]["fixed"] is True                    # the embedder: always on
    assert mc.rows({"up": False}, {}, {}, {}) == []


def test_load_and_unload_rules():
    sent = []
    post = lambda path, body: sent.append((path, body))   # noqa: E731
    served = ["sol-fast", "sol-smart"]
    assert mc.model_op("sol-smart", "load", served, mode="desk", post=post) == "loading sol-smart…"
    for _ in range(50):
        if sent:
            break
        time.sleep(0.02)
    assert sent == [("/models/load", {"model": "sol-smart"})]                          # to the router, in the background
    with pytest.raises(mc.ModelError, match="Away is running"):
        mc.model_op("sol-smart", "load", served, mode="away", post=post)                # the Away job owns the GPU
    with pytest.raises(mc.ModelError, match="off"):
        mc.model_op("sol-smart", "load", served, mode="off", post=post)
    with pytest.raises(mc.ModelError, match="isn't one of"):
        mc.model_op("gpt-5", "load", served, mode="desk", post=post)
    with pytest.raises(mc.ModelError, match="unknown action"):
        mc.model_op("sol-fast", "delete", served, mode="desk", post=post)
    assert mc.unload_all([], mode="desk", post=post) == "nothing is loaded"
    with pytest.raises(mc.ModelError):
        mc.unload_all(["sol-fast"], mode="away", post=post)


def test_ai_power_uses_the_local_ai_script(monkeypatch):
    ran = []
    monkeypatch.setattr(mc, "_mode", lambda: "desk")
    assert "off" in mc.ai_power(False, run=ran.append)
    assert ran[-1][-3:] == ["off", "-Reason", "turned off on the dashboard"]
    mc.ai_power(True, run=ran.append)
    assert ran[-1][-1] == "desk"
    monkeypatch.setattr(mc, "_mode", lambda: "away")
    with pytest.raises(mc.ModelError, match="Stop AI work"):
        mc.ai_power(False, run=ran.append)


def test_through_the_dashboard(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sol_control_hud import hub
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.collector = type("C", (), {"_sampler": type("S", (), {"latest": GPU})(), "get_snapshot": lambda self: None})()
    h.guard = hub.LazyGuard(h.collector._sampler)
    c = TestClient(h.build_app())
    from sol_control_hud.views.web.app import Cached
    h.collectors["llama_swap"] = Cached(lambda: ROUTER, 999)
    h.collectors["model_procs"] = Cached(lambda: PROCS, 999)
    assert [r["id"] for r in h._model_rows()] == ["sol-fast", "sol-smart", "sol-embed"]
    assert c.post("/api/action", json={"action": "model", "target": "sol-fast", "op": "unload"}).status_code == 403
    monkeypatch.setattr(mc, "_mode", lambda: "away")
    r = c.post("/api/action", json={"action": "model", "target": "sol-fast", "op": "unload"}, headers={"X-SOL-Control": "1"}).json()
    assert r["ok"] is False and "Away is running" in r["why"]


def test_vram_budget_probe_reads_the_card():
    from sol_control_hud.data.collectors.budget import vram_budget
    b = vram_budget()
    assert b["available"] and b["vram_gb"] > 8 and 0 < b["budget_gb"] <= b["vram_gb"] + 0.5
