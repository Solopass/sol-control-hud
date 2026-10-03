from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sol_control_hud import hub
from sol_control_hud.data.collectors import notes
from sol_control_hud.data.snapshot import Snapshot

OURS = {"X-SOL-Control": "1"}


class FakeCollector:
    def __init__(self):
        self.snap = Snapshot(ai_state="sleeping", ai_model="sol-fast", sampled_at=1.0)
        self._sampler = type("S", (), {"latest": {"available": False}})()

    def get_snapshot(self):
        return self.snap


class FakeTicker:
    user_hidden = False
    _is_hidden_for_fullscreen = False


@pytest.fixture
def the_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.collector, h.ticker = FakeCollector(), FakeTicker()
    h.guard = hub.LazyGuard(h.collector._sampler)
    return h


def test_notes_api_endpoints(the_hub, tmp_path, monkeypatch):
    # Setup mock vaults
    v1 = tmp_path / "OBVLT"
    v1.mkdir()
    (v1 / "doc1.md").write_text("# Doc One\nContent 1", encoding="utf-8")
    (v1 / "doc2.md").write_text("# Doc Two\nContent 2", encoding="utf-8")

    v2 = tmp_path / "Polymatica Vault"
    v2.mkdir()
    (v2 / "daily.md").write_text("# Daily note", encoding="utf-8")

    monkeypatch.setattr(notes, "discover_vaults", lambda: {"OBVLT": v1, "Polymatica Vault": v2})

    app = the_hub.build_app()
    client = TestClient(app)

    # 1. GET /api/vaults
    res = client.get("/api/vaults")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "OBVLT" in data["vaults"]
    assert "Polymatica Vault" in data["vaults"]
    assert data["default"] == "OBVLT"

    # 2. GET /api/notes for OBVLT
    res = client.get("/api/notes?vault=OBVLT")
    assert res.status_code == 200
    n_data = res.json()
    assert n_data["ok"] is True
    assert n_data["vault"] == "OBVLT"
    assert n_data["count"] == 2
    titles = [n["title"] for n in n_data["notes"]]
    assert "doc1" in titles and "doc2" in titles

    # 3. GET /api/notes with search query
    res = client.get("/api/notes?vault=OBVLT&q=doc1")
    assert res.status_code == 200
    q_data = res.json()
    assert q_data["count"] == 1
    assert q_data["notes"][0]["title"] == "doc1"

    # 4. GET /api/note-content
    res = client.get("/api/note-content?vault=OBVLT&file=doc1.md")
    assert res.status_code == 200
    c_data = res.json()
    assert c_data["ok"] is True
    assert c_data["title"] == "doc1"
    assert "# Doc One" in c_data["text"]

    # 5. POST /api/action open_note
    dispatched = []
    monkeypatch.setattr("os.startfile", lambda uri: dispatched.append(uri))

    act_res = client.post(
        "/api/action",
        json={"action": "open_note", "vault": "OBVLT", "file": "doc1.md"},
        headers=OURS,
    )
    assert act_res.status_code == 200
    act_data = act_res.json()
    assert act_data["ok"] is True
    assert len(dispatched) == 1
    assert "obsidian://open?vault=OBVLT&file=doc1.md" in dispatched[0]
