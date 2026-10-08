"""The dashboard's Settings panel: what it reads, what it accepts, and that ticker changes reach the Tk thread whole."""
import queue

import pytest

from sol_control_hud import hub, settings_api
from sol_control_hud.views.settings_dialog import PRESETS


class FakeTicker:
    def __init__(self):
        self.settings = {"interval_seconds": 6, "poll_pace": 2.0, "scan_interval": 60, "stability_interval": 300,
                         "monitor_self": True, "alerts_sound": True, "theme": "cyber-cyan"}
        self.slides_enabled = {"HW": True, "AI": True}
        self.font_scale = "normal"
        self.user_hidden = False
        self.theme_name = "cyber-cyan"


@pytest.fixture
def the_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.ticker = FakeTicker()
    return h


def drain(h):
    out = []
    while True:
        try:
            out.append(h.cmds.get_nowait())
        except queue.Empty:
            return out


def test_read_lists_everything(the_hub):
    d = settings_api.read(the_hub)
    assert set(d["app"]) == {"ticker_shown", "start_at_login", "dashboard_at_start", "notify", "hud_heal"}
    assert d["app"]["hud_heal"] is False
    t = d["ticker"]
    assert t["preset"] == "balanced" and t["interval_seconds"] == 6 and t["alerts_sound"] is True
    assert any(s["tag"] == "APPS" for s in t["slides"]) and {p["key"] for p in t["presets"]} == set(PRESETS)


def test_ticker_changes_go_whole_to_the_tk_thread(the_hub):
    """apply_new_settings treats a missing key as the default: a lone {"interval_seconds": 8} would reset the pace,
    the alert sound and the rest. So the panel always sends the full settings with the one change merged in."""
    assert settings_api.change(the_hub, "interval_seconds", "8")["ok"]
    (name, arg), = drain(the_hub)
    assert name == "ticker_settings" and arg["interval_seconds"] == 8 and arg["alerts_sound"] is True and arg["poll_pace"] == 2.0
    settings_api.change(the_hub, "preset", "eco")
    (name, arg), = drain(the_hub)
    assert arg["poll_pace"] == PRESETS["eco"]["poll_pace"] and arg["alerts_sound"] is True
    settings_api.change(the_hub, "slide:HW", False)
    assert drain(the_hub) == [("slide", ("HW", False))]
    settings_api.change(the_hub, "font_scale", "small")
    assert drain(the_hub) == [("font_scale", "small")]


def test_bad_values_are_refused(the_hub):
    for key, value in (("interval_seconds", "1"), ("interval_seconds", "x"), ("preset", "ludicrous"),
                       ("font_scale", "huge"), ("slide:NOPE", True), ("hud_heal", "maybe"), ("rm -rf", True)):
        with pytest.raises(settings_api.SettingError):
            settings_api.change(the_hub, key, value)
    the_hub.ticker.slides_enabled = {"HW": True, **{t: False for t, _ in settings_api.SLIDE_LABELS if t != "HW"}}
    with pytest.raises(settings_api.SettingError, match="at least one"):
        settings_api.change(the_hub, "slide:HW", False)
    assert drain(the_hub) == []


def test_hud_heal_is_saved(the_hub):
    assert settings_api.change(the_hub, "hud_heal", True)["ok"]
    assert hub.load_settings().get("hud_heal") is True


class FakeRegistry:
    """Never the machine's own registry: these tests would otherwise repin the real apps."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def read(self):
        return dict(self.values)

    def write(self, name, value):
        self.values[name] = value
        self.writes.append((name, value))


@pytest.fixture
def fake_gpu(monkeypatch):
    from sol_control_hud import gpu_prefs
    reg = FakeRegistry()
    apps = [{"key": "brave", "label": "Brave", "paths": [r"C:\x\brave.exe"], "about": "browser"},
            {"key": "discord", "label": "Discord", "paths": [r"C:\x\Discord.exe"], "about": "chat"}]
    monkeypatch.setattr(gpu_prefs, "APPS", apps)
    monkeypatch.setattr(gpu_prefs, "app_paths", lambda a: list(a["paths"]))
    monkeypatch.setattr(gpu_prefs, "Registry", lambda: reg)
    monkeypatch.setattr(gpu_prefs, "intel_available", lambda adapters=None: True)
    return reg


def test_one_app_is_set_by_its_own_paths_and_remembered(the_hub, fake_gpu):
    r = settings_api.change(the_hub, "gpu:brave", "amd")
    assert r["ok"] and "Brave" in r["why"]
    assert fake_gpu.writes == [(r"C:\x\brave.exe", "GpuPreference=2;")]
    assert hub.load_settings()["gpu_choices"] == {"brave": "amd"}      # survives a restart: reapply() reads this


def test_all_apps_at_once(the_hub, fake_gpu):
    settings_api.change(the_hub, "gpu:all", "amd")
    assert {n for n, _ in fake_gpu.writes} == {r"C:\x\brave.exe", r"C:\x\Discord.exe"}
    assert hub.load_settings()["gpu_choices"] == {"brave": "amd", "discord": "amd"}


def test_other_settings_in_the_value_string_survive(the_hub, fake_gpu):
    """Windows keeps Auto HDR and the windowed-game toggle in the same value; 10-08 one was deleted by mistake."""
    fake_gpu.values[r"C:\x\brave.exe"] = "AutoHDREnable=1;GpuPreference=1;"
    settings_api.change(the_hub, "gpu:brave", "amd")
    assert "AutoHDREnable=1" in fake_gpu.values[r"C:\x\brave.exe"]
    assert "GpuPreference=2" in fake_gpu.values[r"C:\x\brave.exe"]


def test_a_bad_app_or_choice_writes_nothing(the_hub, fake_gpu):
    for key, value in (("gpu:brave", "radeon"), ("gpu:solitaire", "amd")):
        with pytest.raises(settings_api.SettingError):
            settings_api.change(the_hub, key, value)
    assert fake_gpu.writes == []


def test_intel_is_refused_when_the_chip_is_off(the_hub, fake_gpu, monkeypatch):
    """The panel disables the option too, but the server decides: the open page may be stale."""
    from sol_control_hud import gpu_prefs
    monkeypatch.setattr(gpu_prefs, "intel_available", lambda adapters=None: False)
    with pytest.raises(settings_api.SettingError, match="iGPU Multi-Monitor"):
        settings_api.change(the_hub, "gpu:brave", "intel")
    assert fake_gpu.writes == []


def test_the_panel_reads_back_what_it_set(the_hub, fake_gpu):
    settings_api.change(the_hub, "gpu:brave", "amd")
    rows = {a["key"]: a for a in settings_api.read(the_hub)["gpu"]["apps"]}
    assert rows["brave"]["choice"] == "amd" and rows["discord"]["choice"] == "auto"
    assert "running" in rows["brave"]                                  # the panel's "applies on next start" marker


def test_setting_action_needs_our_header(the_hub):
    from fastapi.testclient import TestClient
    from sol_control_hud.data.snapshot import Snapshot
    the_hub.collector = type("C", (), {"get_snapshot": lambda self: Snapshot(), "_sampler": type("S", (), {"latest": {}})()})()
    the_hub.guard = hub.LazyGuard(the_hub.collector._sampler)
    client = TestClient(the_hub.build_app())
    body = {"action": "setting", "target": "interval_seconds", "value": 9}
    assert client.post("/api/action", json=body).status_code == 403
    assert client.post("/api/action", json=body, headers={"X-SOL-Control": "1"}).json()["ok"]
    assert client.get("/api/settings").json()["ticker"]["interval_seconds"] == 6       # applied later, on the Tk thread
