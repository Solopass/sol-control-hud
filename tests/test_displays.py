"""Rendering on a GPU with no monitor: the check that would have caught 10-08 while it was happening.

Both halves matter and the tests keep them apart: the Windows setting is true even while an app is shut, and the
live counters are true even after the setting has been put back (a running app keeps its old adapter until restart,
which is exactly the state this machine was in once the preferences were cleared).
"""
import pytest

from sol_control_hud import doctor
from sol_control_hud.data.collectors import displays

AMD = "luid_0x00000000_0x0000ec18"
INTEL = "luid_0x00000000_0x0041f4d1"
BASIC = "luid_0x00000000_0x000109f0"

ADAPTERS = [
    {"luid": AMD, "name": "AMD Radeon RX 9070 XT", "vendor": 0x1002, "has_display": True, "software": False},
    {"luid": INTEL, "name": "Intel(R) UHD Graphics 770", "vendor": 0x8086, "has_display": False, "software": False},
    {"luid": BASIC, "name": "Microsoft Basic Render Driver", "vendor": 0x1414, "has_display": False, "software": True},
]


def util(*entries) -> dict[str, float]:
    """Counter paths the way Windows spells them: (pid, luid, engine, percent)."""
    return {rf"\GPU Engine(pid_{pid}_{luid}_phys_0_eng_{i}_engtype_{eng})\Utilization Percentage": pct
            for i, (pid, luid, eng, pct) in enumerate(entries)}


def names(mapping):
    return lambda pid: mapping.get(pid, f"pid {pid}")


# ---- joining adapters to monitors

def test_an_adapter_is_dark_when_no_monitor_carries_its_name():
    listing = lambda: [{"luid": AMD, "name": "AMD Radeon RX 9070 XT", "vendor": 0x1002},            # noqa: E731
                       {"luid": INTEL, "name": "Intel(R) UHD Graphics 770", "vendor": 0x8086}]
    got = displays.adapters(attached={"AMD Radeon RX 9070 XT"}, listing=listing)
    assert [a["has_display"] for a in got] == [True, False]


def test_the_software_renderer_is_never_a_problem():
    listing = lambda: [{"luid": BASIC, "name": "Microsoft Basic Render Driver", "vendor": 0x1414}]  # noqa: E731
    got = displays.adapters(attached=set(), listing=listing)
    assert got[0]["software"] is True
    assert displays.misplaced(util((1, BASIC, "3D", 50.0)), got, names({1: "whatever"})) == []


# ---- what is rendering where

def test_work_on_a_dark_adapter_is_reported_busiest_first():
    rows = displays.misplaced(
        util((100, INTEL, "3D", 22.2), (200, INTEL, "3D", 2.5), (300, AMD, "3D", 80.0)),
        ADAPTERS, names({100: "Antigravity", 200: "Discord", 300: "RocketLeague"}))
    assert [(r["name"], r["percent"], r["busy"]) for r in rows] == [("Antigravity", 22.2, True), ("Discord", 2.5, True)]
    assert rows[0]["adapter"] == "Intel(R) UHD Graphics 770"      # work on the AMD is where it belongs: not listed


def test_one_app_spread_over_many_processes_is_one_row():
    """Chromium apps run a dozen processes; a row each would say the same thing twelve times."""
    rows = displays.misplaced(
        util((11, INTEL, "3D", 4.0), (12, INTEL, "3D", 1.0), (13, INTEL, "3D", 0.5)),
        ADAPTERS, names({11: "Antigravity", 12: "Antigravity", 13: "Antigravity"}))
    assert len(rows) == 1
    assert rows[0] == {"name": "Antigravity", "percent": 5.5, "processes": 3,
                       "adapter": "Intel(R) UHD Graphics 770", "busy": True}


def test_only_3d_work_counts_and_an_idle_context_is_not_busy():
    rows = displays.misplaced(
        util((100, INTEL, "VideoDecode", 40.0), (200, INTEL, "3D", 0.0)),
        ADAPTERS, names({100: "brave", 200: "Obsidian"}))
    assert [r["name"] for r in rows] == ["Obsidian"]               # decode elsewhere is normal; 3D is not
    assert rows[0]["busy"] is False                                # a context is open, nothing is being drawn


def test_processes_that_hold_a_context_everywhere_are_not_the_problem():
    rows = displays.misplaced(
        util((1, INTEL, "3D", 3.0), (2, INTEL, "3D", 3.0), (3, INTEL, "3D", 3.0)),
        ADAPTERS, names({1: "dwm", 2: "System", 3: "llama-server"}))
    assert rows == []


def test_nothing_is_reported_when_every_adapter_drives_a_screen():
    lit = [{**a, "has_display": True} for a in ADAPTERS]
    assert displays.misplaced(util((100, INTEL, "3D", 90.0)), lit, names({100: "Antigravity"})) == []


# ---- the two signals together

def test_the_setting_alone_is_enough_even_with_nothing_running():
    r = displays.report(util=None, known=ADAPTERS,
                        prefs=[{"app": "Discord.exe", "preference": 1, "words": "power saving"},
                               {"app": "brave.exe", "preference": 2, "words": "high performance"}])
    assert r["setting_problem"] is True
    assert r["power_saving_apps"] == ["Discord.exe"]               # only the power-saving pins
    assert r["dark"] == ["Intel(R) UHD Graphics 770"] and r["rendering"] == []


def test_a_power_saving_pin_is_fine_when_there_is_only_one_chip():
    only_amd = [ADAPTERS[0]]
    r = displays.report(util=None, known=only_amd, prefs=[{"app": "Discord.exe", "preference": 1, "words": "power saving"}])
    assert r["setting_problem"] is False                           # nowhere else for it to land


def test_the_state_after_clearing_the_setting_but_before_restarting_the_apps():
    """Exactly where this machine stood on 10-08: registry clean, Antigravity still on the Intel."""
    r = displays.report(util=util((100, INTEL, "3D", 22.2)), name_of=names({100: "Antigravity"}),
                        known=ADAPTERS, prefs=[])
    assert r["setting_problem"] is False                           # nothing is pinned any more
    assert [x["name"] for x in r["busy"]] == ["Antigravity"]       # but it is still rendering on the dark chip


def test_the_global_toggles_are_not_an_app(tmp_path, monkeypatch):
    """That key also holds DirectXUserGlobalSettings (windowed-game optimizations, Auto HDR). It is not a pinned
    app and must never be listed as one - 10-08 it was deleted along with the pins by mistake."""
    import winreg
    key = r"Software\SolControlHudTests\UserGpuPreferences"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, "DirectXUserGlobalSettings", 0, winreg.REG_SZ, "SwapEffectUpgradeEnable=1;AutoHDREnable=1;")
        winreg.SetValueEx(k, r"C:\apps\Discord.exe", 0, winreg.REG_SZ, "GpuPreference=1;")
    try:
        prefs = displays.gpu_preferences(key)
        assert [p["app"] for p in prefs] == ["Discord.exe"]
        assert displays.report(util=None, known=ADAPTERS, prefs=prefs)["power_saving_apps"] == ["Discord.exe"]
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\SolControlHudTests")


def test_the_software_renderer_is_left_out_of_the_adapter_list():
    r = displays.report(util=None, known=ADAPTERS, prefs=[])
    assert [a["name"] for a in r["adapters"]] == ["AMD Radeon RX 9070 XT", "Intel(R) UHD Graphics 770"]


# ---- sol-doctor

def _levels(status):
    return {name: (level, detail) for name, level, detail in doctor.checks(status)}


@pytest.fixture
def status():
    """The smallest status sol-doctor's other checks tolerate, so these tests are about the display check only."""
    return {"llama_swap": {}, "ollama": {}, "backend": {}, "gpu": {"available": False}, "vram": {},
            "speed": {}, "memory": {"percent": 40, "used_gb": 30, "total_gb": 96}, "disks": [],
            "backups": {"latest": "x.tar", "age_hours": 1}, "wsl": {"available": False}, "stability": {"available": False}}


def test_doctor_warns_while_apps_render_on_the_dark_chip(status):
    status["displays"] = displays.report(util=util((100, INTEL, "3D", 22.2)), name_of=names({100: "Antigravity"}),
                                         known=ADAPTERS, prefs=[])
    level, detail = _levels(status)["  Rendering on the wrong GPU"]
    assert level == doctor.WARN
    assert "Antigravity 22.2%" in detail and "no monitor" in detail


def test_doctor_warns_on_the_setting_before_anything_runs(status):
    status["displays"] = displays.report(util=None, known=ADAPTERS,
                                         prefs=[{"app": "Discord.exe", "preference": 1, "words": "power saving"}])
    level, detail = _levels(status)["  Rendering on the wrong GPU"]
    assert level == doctor.WARN and "power saving" in detail and "Discord.exe" in detail


def test_doctor_is_ok_when_the_dark_chip_is_simply_unused(status):
    status["displays"] = displays.report(util={}, known=ADAPTERS, prefs=[])
    level, detail = _levels(status)["  Rendering on the wrong GPU"]
    assert level == doctor.OK and "nothing is rendering on it" in detail


def test_doctor_says_nothing_when_there_is_no_display_data(status):
    assert "  Rendering on the wrong GPU" not in _levels(status)
