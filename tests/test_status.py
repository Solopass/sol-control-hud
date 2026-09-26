import time

from fastapi.testclient import TestClient

from sol_control_hud.views.web.app import Cached, create_app
from sol_control_hud.data.collectors import system


def fake(value):
    return Cached(lambda: value, ttl=0)


def boom():
    raise RuntimeError("collector exploded")


def test_status_contains_every_collector_and_survives_a_broken_one():
    collectors = {
        "ollama": fake({"up": False}),
        "gpu": Cached(boom, ttl=0),
        "memory": fake({"total_gb": 96, "used_gb": 10, "percent": 10.4}),
    }
    client = TestClient(create_app(collectors=collectors))
    body = client.get("/api/status").json()
    assert body["ollama"] == {"up": False}
    assert body["memory"]["total_gb"] == 96
    assert body["gpu"]["available"] is False and "collector exploded" in body["gpu"]["error"]
    assert body["generated_at"] <= time.time()


def test_index_served():
    client = TestClient(create_app(collectors={}))
    r = client.get("/")
    assert r.status_code == 200 and "SOL Control HUD" in r.text


def test_cached_respects_ttl():
    calls = []
    c = Cached(lambda: calls.append(1) or len(calls), ttl=60)
    assert c.get() == 1 and c.get() == 1 and len(calls) == 1


def test_parse_wsl_list_handles_default_marker_and_nulls():
    text = "  NAME            STATE           VERSION\n* Ubuntu-24.04    Running         2\n  Other           Stopped         2\n"
    assert system.parse_wsl_list(text.replace("U", "U\x00")) == {"Ubuntu-24.04": "Running", "Other": "Stopped"}


def test_engines_report_down_when_nothing_listens(monkeypatch):
    from sol_control_hud.data.collectors import engines
    monkeypatch.setattr(engines, "OLLAMA", "http://127.0.0.1:9")
    monkeypatch.setattr(engines, "LLAMA_SWAP", "http://127.0.0.1:9")
    assert engines.ollama(timeout=0.5) == {"up": False}
    assert engines.llama_swap(timeout=0.5) == {"up": False}


def test_backups_none_when_missing(tmp_path):
    assert system.backups(str(tmp_path / "nope-*.tar")) == {"latest": None, "age_hours": None}
