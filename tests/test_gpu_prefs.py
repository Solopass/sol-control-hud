"""Which graphics chip each everyday app uses (gpu_prefs.py). A fake registry and fake install folders: the real
HKCU key is never written by these tests."""
import pytest

from sol_control_hud import gpu_prefs, hub, settings_api


class FakeRegistry:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def read(self):
        return dict(self.values)

    def write(self, name, value):
        self.writes.append((name, value))
        self.values[name] = value


@pytest.fixture
def apps(tmp_path, monkeypatch):
    """Brave, Steam (two programs) and Discord (versioned folders) installed under tmp_path."""
    brave = tmp_path / "Brave" / "brave.exe"
    steam, helper = tmp_path / "Steam" / "steam.exe", tmp_path / "Steam" / "cef" / "steamwebhelper.exe"
    for p in (brave, steam, helper):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    for v in ("1.0.9259", "1.0.9260", "1.0.10001"):
        d = tmp_path / "Discord" / f"app-{v}"
        d.mkdir(parents=True)
        (d / "Discord.exe").write_text("")
    monkeypatch.setattr(gpu_prefs, "LOCAL", str(tmp_path))
    monkeypatch.setattr(gpu_prefs, "APPS", [
        {"key": "brave", "label": "Brave", "paths": [str(brave)], "about": "browser"},
        {"key": "steam", "label": "Steam", "paths": [str(steam), str(helper)], "about": "store"},
        {"key": "discord", "label": "Discord", "paths": ["<discord>"], "about": "chat"},
        {"key": "missing", "label": "Not installed", "paths": [str(tmp_path / "nope.exe")], "about": ""},
    ])
    return {"brave": str(brave), "steam": str(steam), "helper": str(helper),
            "discord": str(tmp_path / "Discord" / "app-1.0.10001" / "Discord.exe")}


def test_value_strings_keep_other_settings():
    assert gpu_prefs.parse_pref("GpuPreference=2;") == 2 and gpu_prefs.parse_pref("") is None
    assert gpu_prefs.with_pref("SwapEffectUpgradeEnable=1;GpuPreference=2;", 1) == "GpuPreference=1;SwapEffectUpgradeEnable=1;"
    assert gpu_prefs.with_pref(None, 0) == "GpuPreference=0;"


def test_discord_newest_version_folder(apps):
    assert gpu_prefs._discord_path() == apps["discord"]          # 1.0.10001 > 1.0.9260: compared as numbers


def test_state_lists_installed_apps_with_their_choice(apps):
    reg = FakeRegistry({apps["brave"]: "GpuPreference=2;", apps["steam"]: "GpuPreference=1;",
                        apps["helper"]: "GpuPreference=1;", r"C:\ASUS\asus_framework.exe": "GpuPreference=1; "})
    s = gpu_prefs.state(reg, adapters=[{"vendor": 0x1002}], running={apps["brave"].lower()})
    rows = {r["key"]: r for r in s["apps"]}
    assert s["intel_available"] is False and "missing" not in rows
    assert rows["brave"]["choice"] == "amd" and rows["brave"]["running"] is True
    assert rows["steam"]["choice"] == "intel" and rows["discord"]["choice"] == "auto"
    assert gpu_prefs.intel_available([{"vendor": 0x8086}, {"vendor": 0x1002}]) is True


def test_set_choice_writes_every_program_of_an_app_and_nothing_else(apps):
    reg = FakeRegistry({r"C:\ASUS\asus_framework.exe": "GpuPreference=1; "})
    assert gpu_prefs.set_choice(["steam", "discord"], "intel", reg) == ["Steam", "Discord"]
    assert sorted(n for n, _ in reg.writes) == sorted([apps["steam"], apps["helper"], apps["discord"]])
    assert all(v == "GpuPreference=1;" for _, v in reg.writes)
    assert reg.values[r"C:\ASUS\asus_framework.exe"] == "GpuPreference=1; "            # someone else's: untouched
    with pytest.raises(ValueError):
        gpu_prefs.set_choice(["brave"], "nvidia", reg)
    with pytest.raises(ValueError):
        gpu_prefs.set_choice([r"C:\Windows\explorer.exe"], "intel", reg)               # only listed apps, by key


def test_reapply_follows_discord_to_its_new_folder(apps, tmp_path):
    reg = FakeRegistry()
    gpu_prefs.set_choice(["discord"], "intel", reg)
    d = tmp_path / "Discord" / "app-1.0.10002"                                         # Discord updated
    d.mkdir()
    (d / "Discord.exe").write_text("")
    gpu_prefs.reapply({"discord": "intel", "bogus": "intel", "brave": "nonsense"}, reg)
    assert reg.values[str(d / "Discord.exe")] == "GpuPreference=1;"


def test_settings_panel_refuses_intel_while_the_chip_is_off(apps, tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), False, False)
    reg = FakeRegistry()
    monkeypatch.setattr(gpu_prefs, "Registry", lambda: reg)
    monkeypatch.setattr(gpu_prefs, "intel_available", lambda adapters=None: False)
    with pytest.raises(settings_api.SettingError, match="BIOS"):
        settings_api.change(h, "gpu:brave", "intel")
    assert reg.writes == []
    assert settings_api.change(h, "gpu:brave", "auto")["ok"]                          # back to Windows' choice is fine
    monkeypatch.setattr(gpu_prefs, "intel_available", lambda adapters=None: True)
    r = settings_api.change(h, "gpu:all", "intel")
    assert r["ok"] and "Restart them" in r["why"]
    assert hub.load_settings()["gpu_choices"] == {"brave": "intel", "steam": "intel", "discord": "intel"}
    with pytest.raises(settings_api.SettingError):
        settings_api.change(h, "gpu:missing", "amd")                                  # not installed
