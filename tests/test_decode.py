"""Spotting a browser decoding video on the CPU — the 10-08 Brave failure, which nothing noticed at the time.

The signature it looks for is the one that was actually measured that day: the whole machine's video decode engine
idle, Brave burning 37 % of a core, and the card at 3 %. The tests below are mostly about *not* firing, because a
check that cries wolf on a busy web page would be worse than no check.
"""
import pytest

from sol_control_hud import doctor
from sol_control_hud.data.collectors import decode

AMD = "luid_0x00000000_0x0000eb36"


def util(*entries) -> dict[str, float]:
    """(pid, engine, percent) as Windows spells the counter paths."""
    return {rf"\GPU Engine(pid_{pid}_{AMD}_phys_0_eng_{i}_engtype_{eng})\Utilization Percentage": pct
            for i, (pid, eng, pct) in enumerate(entries)}


@pytest.fixture(autouse=True)
def names(monkeypatch):
    """pid 100 is brave, 200 is chrome, 300 is something else."""
    monkeypatch.setattr(decode.gpu, "process_name",
                        lambda pid, cache=None: {100: "brave", 200: "chrome"}.get(pid, "notepad"))


def test_the_10_08_signature_fires_after_enough_samples():
    w = decode.SoftwareDecodeWatch(needed=3)
    u = util((100, "3D", 4.7))                       # drawing, and nothing on videodecode anywhere
    assert w.update(u, {"brave": 0.0}, now=0.0)["suspects"] == []        # first sample: no rate yet
    for i, t in enumerate((10.0, 20.0), start=1):
        out = w.update(u, {"brave": 3.7 * i}, now=t)                     # 37 % of one core
        assert out["suspects"] == [] and out["apps"][0]["samples"] == i   # building up, not yet sure
    out = w.update(u, {"brave": 11.1}, now=30.0)
    assert [s["name"] for s in out["suspects"]] == ["brave"]
    assert out["apps"][0]["cpu_percent"] == 37.0 and out["hardware_decode_percent"] == 0.0


def test_it_stays_quiet_when_the_gpu_is_doing_the_decoding():
    """The normal case: hardware decode is working, so CPU use is none of our business."""
    w = decode.SoftwareDecodeWatch(needed=1)
    u = util((100, "3D", 4.7), (100, "VideoDecode", 12.0))
    w.update(u, {"brave": 0.0}, now=0.0)
    out = w.update(u, {"brave": 5.0}, now=10.0)
    assert out["suspects"] == [] and out["hardware_decode_percent"] == 12.0


def test_it_stays_quiet_when_nothing_is_playing():
    """No video: the decode engine is idle too, but the browser is not burning anything."""
    w = decode.SoftwareDecodeWatch(needed=1)
    u = util((100, "3D", 4.7))
    w.update(u, {"brave": 0.0}, now=0.0)
    assert w.update(u, {"brave": 0.2}, now=10.0)["suspects"] == []        # 2 % of a core


def test_it_stays_quiet_for_cpu_with_nothing_on_screen():
    """A background tab burning CPU is not video decode: there is no drawing to go with it."""
    w = decode.SoftwareDecodeWatch(needed=1)
    u = util((100, "3D", 0.0))
    w.update(u, {"brave": 0.0}, now=0.0)
    assert w.update(u, {"brave": 9.0}, now=10.0)["suspects"] == []


def test_a_burst_does_not_count_and_the_streak_resets():
    w = decode.SoftwareDecodeWatch(needed=3)
    busy, idle = util((100, "3D", 4.7)), util((100, "3D", 4.7))
    w.update(busy, {"brave": 0.0}, now=0.0)
    assert w.update(busy, {"brave": 5.0}, now=10.0)["apps"][0]["samples"] == 1
    assert w.update(idle, {"brave": 5.1}, now=20.0)["apps"][0]["samples"] == 0   # quiet sample breaks it
    assert w.update(busy, {"brave": 10.1}, now=30.0)["apps"][0]["samples"] == 1


def test_only_browsers_and_players_are_watched():
    w = decode.SoftwareDecodeWatch(needed=1)
    u = util((300, "3D", 40.0))
    w.update(u, {"notepad": 0.0}, now=0.0)
    out = w.update(u, {"notepad": 20.0}, now=10.0)
    assert out["apps"] == [] and out["suspects"] == []


def test_an_app_that_closes_is_forgotten():
    w = decode.SoftwareDecodeWatch(needed=2)
    u = util((100, "3D", 4.7))
    w.update(u, {"brave": 0.0}, now=0.0)
    w.update(u, {"brave": 5.0}, now=10.0)
    assert w.streak["brave"] == 1
    w.update(u, {}, now=20.0)
    assert "brave" not in w.streak


def test_two_browsers_are_judged_separately():
    w = decode.SoftwareDecodeWatch(needed=1)
    u = util((100, "3D", 4.7), (200, "3D", 4.0))
    w.update(u, {"brave": 0.0, "chrome": 0.0}, now=0.0)
    out = w.update(u, {"brave": 5.0, "chrome": 0.1}, now=10.0)
    assert [s["name"] for s in out["suspects"]] == ["brave"]


# ---- sol-doctor

def _levels(status):
    return {name: (level, detail) for name, level, detail in doctor.checks(status)}


@pytest.fixture
def status():
    return {"llama_swap": {}, "ollama": {}, "backend": {}, "gpu": {"available": False}, "vram": {}, "speed": {},
            "memory": {"percent": 40, "used_gb": 30, "total_gb": 96}, "disks": [],
            "backups": {"latest": "x.tar", "age_hours": 1}, "wsl": {"available": False}, "stability": {"available": False}}


def test_doctor_warns_with_the_app_and_the_number(status):
    status["decode"] = {"available": True, "suspects": [{"name": "brave", "cpu_percent": 37.0, "gpu_3d": 4.7, "samples": 3}]}
    level, detail = _levels(status)["  Video decoding on the CPU"]
    assert level == doctor.WARN and "brave" in detail and "37.0%" in detail and "software decode" in detail


def test_doctor_says_nothing_when_there_is_no_suspect(status):
    status["decode"] = {"available": True, "suspects": [], "apps": []}
    assert "  Video decoding on the CPU" not in _levels(status)
    assert "  Video decoding on the CPU" not in _levels({**status, "decode": {}})
