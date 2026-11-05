import datetime
import json
import pytest
from fastapi.testclient import TestClient

from sol_control_hud import hub
from sol_control_hud.data.collectors import ai_usage


def test_ai_usage_collector_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_usage, "SUMMARY_FILE", tmp_path / "nonexistent.json")
    res = ai_usage.get_ai_usage()
    assert res["available"] is False
    assert "not found" in res["why"]


def test_ai_usage_collector_with_data(monkeypatch, tmp_path):
    today_str = datetime.date.today().isoformat()
    summary_path = tmp_path / "summary.json"
    data = {
        "week_id": "2026-W45",
        "last_updated": f"{today_str} 17:30",
        "weekly_totals": {
            "total_turns": 500,
            "claude": {"exact_tokens": 1200000, "turns": 150},
            "antigravity": {"estimated_tokens": 800000, "turns": 350},
        },
        "daily": {
            today_str: {
                "date": today_str,
                "providers": {
                    "claude": {
                        "turn_count": 40,
                        "input_tokens": 100,
                        "output_tokens": 5000,
                        "cache_read_tokens": 20000,
                        "thinking_tokens": 1200,
                        "tool_calls": 12,
                    },
                    "antigravity": {
                        "turn_count": 80,
                        "input_tokens": 200,
                        "output_tokens": 15000,
                        "tool_calls": 25,
                    },
                },
                "models": [{"model": "Gemini 3.8 Flash", "turns": 80}],
                "projects": ["sol-control-hud"],
            }
        },
    }
    summary_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(ai_usage, "SUMMARY_FILE", summary_path)

    res = ai_usage.get_ai_usage()
    assert res["available"] is True
    assert res["week_id"] == "2026-W45"
    assert res["weekly"]["claude_tokens"] == 1200000
    assert res["today"]["claude"]["turns"] == 40
    assert res["today"]["claude"]["cache_read_tokens"] == 20000
    assert res["today"]["antigravity"]["turns"] == 80


def test_ai_usage_api_endpoint(monkeypatch, tmp_path):
    # Setup test hub
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.collector = type("C", (), {"_sampler": type("S", (), {"latest": {}})(), "health_alert": ""})()
    h.ticker = type("T", (), {"user_hidden": False, "_is_hidden_for_fullscreen": False, "theme_name": "nordic-frost"})()
    h.guard = hub.LazyGuard(h.collector._sampler)

    # Mock collector data
    monkeypatch.setattr(ai_usage, "get_ai_usage", lambda: {"available": True, "week_id": "2026-W45", "today": {}, "weekly": {}})

    app = h.build_app()
    client = TestClient(app)
    r = client.get("/api/ai-usage")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["week_id"] == "2026-W45"
