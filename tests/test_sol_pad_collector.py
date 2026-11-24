from sol_control_hud.data.collectors import sol_pad


def test_get_sol_pad_status_states(monkeypatch):
    monkeypatch.setattr(sol_pad, "_usbmux_ipad_attached", lambda: True)
    monkeypatch.setattr(sol_pad, "_mutex_exists", lambda name: True)
    st = sol_pad.get_sol_pad_status()
    assert st["state"] == "online"
    assert st["usb_connected"] is True
    assert st["stream_running"] is True
    assert st["dock_running"] is True

    monkeypatch.setattr(sol_pad, "_mutex_exists", lambda name: "Stream" in name)
    st2 = sol_pad.get_sol_pad_status()
    assert st2["state"] == "partial"

    monkeypatch.setattr(sol_pad, "_mutex_exists", lambda name: False)
    st3 = sol_pad.get_sol_pad_status()
    assert st3["state"] == "standby"

    monkeypatch.setattr(sol_pad, "_usbmux_ipad_attached", lambda: False)
    st4 = sol_pad.get_sol_pad_status()
    assert st4["state"] == "offline"


def test_open_popos_rdp(monkeypatch, tmp_path):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))

    fake_moonlight = tmp_path / "Moonlight.exe"
    fake_moonlight.write_text("", encoding="utf-8")
    monkeypatch.setattr(sol_pad, "MOONLIGHT_EXE", fake_moonlight)
    monkeypatch.setattr(sol_pad.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=47989, timeout=0.35: True)
    res = sol_pad.open_popos_rdp()
    assert res["ok"] is True
    assert "opened Moonlight" in res["why"]
    assert calls[0][0] == [str(fake_moonlight), "--display-mode", "windowed", "--absolute-mouse", "stream", "popos.lan", "Desktop"]
    assert calls[0][1]["cwd"] == str(tmp_path)

    # Fallback to mstsc.exe if Moonlight is missing
    monkeypatch.setattr(sol_pad, "MOONLIGHT_EXE", tmp_path / "missing.exe")
    res_rdp = sol_pad.open_popos_rdp()
    assert res_rdp["ok"] is True
    assert "opened Remote Desktop" in res_rdp["why"]
    assert calls[1][0] == ["mstsc.exe", "/v:popos.lan"]

    # When sleeping, sends WoL and starts background waiter thread
    wols = []
    threads = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=False):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            threads.append((self.target, self.args, self.daemon))

    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=3389, timeout=0.35: False)
    monkeypatch.setattr(sol_pad, "send_wol", lambda mac=sol_pad.POPOS_MAC: wols.append(mac))
    monkeypatch.setattr(sol_pad.threading, "Thread", FakeThread)
    res_wake = sol_pad.open_popos_rdp()
    assert res_wake["ok"] is True
    assert "waking" in res_wake["why"]
    assert wols == [sol_pad.POPOS_MAC]
    assert len(threads) == 1 and threads[0][2] is True

    def fail_popen(cmd, **kwargs):
        raise OSError("not found")

    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=3389, timeout=0.35: True)
    monkeypatch.setattr(sol_pad.subprocess, "Popen", fail_popen)
    err = sol_pad.open_popos_rdp()
    assert err["ok"] is False
    assert "not found" in err["why"]


def test_popos_status_wake_and_sleep(monkeypatch, tmp_path):
    sol_pad._popos_cache["at"] = 0.0
    sol_pad._popos_cache["data"] = {}
    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=22, timeout=0.25: False)
    st_off = sol_pad.get_popos_status()
    assert st_off == {"online": False, "locked": False, "state": "sleeping"}

    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=22, timeout=0.25: port == sol_pad.POPOS_DROPBEAR_PORT)
    st_locked = sol_pad.get_popos_status()
    assert st_locked == {"online": False, "locked": True, "state": "locked"}

    luks_file = tmp_path / "popos_luks.txt"
    luks_file.write_text("testpass\n", encoding="utf-8")
    monkeypatch.setattr(sol_pad, "POPOS_LUKS_FILE", luks_file)

    class FakeProc:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    unlock_inputs = []
    monkeypatch.setattr(
        sol_pad.subprocess,
        "run",
        lambda cmd, **kw: (unlock_inputs.append((cmd, kw.get("input"))), FakeProc(0, "", ""))[1],
    )
    ul = sol_pad.unlock_popos_luks()
    assert ul["ok"] is True
    assert unlock_inputs[0][1] == "testpass\n"
    assert str(sol_pad.POPOS_DROPBEAR_PORT) in unlock_inputs[0][0]

    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=22, timeout=0.25: True)
    monkeypatch.setattr(
        sol_pad.subprocess,
        "run",
        lambda *a, **kw: FakeProc(0, "0.25\n8.3\n29\n2\n", ""),
    )
    st_on = sol_pad.get_popos_status()
    assert st_on["online"] is True
    assert st_on["state"] == "awake"
    assert st_on["mem_avail_gb"] == 8.3
    assert st_on["gpu_temp_c"] == 29
    assert st_on["docker_running"] == 2

    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=22, timeout=0.25: False)
    wols = []
    monkeypatch.setattr(sol_pad, "send_wol", lambda mac=sol_pad.POPOS_MAC: wols.append(mac))
    monkeypatch.setattr(sol_pad.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())
    w = sol_pad.wake_popos()
    assert w["ok"] is True and wols == [sol_pad.POPOS_MAC]

    sleep_calls = []

    def fake_sleep_run(cmd, **kwargs):
        sleep_calls.append(cmd)
        return FakeProc(0, "", "")

    monkeypatch.setattr(sol_pad.subprocess, "run", fake_sleep_run)
    s = sol_pad.sleep_popos()
    assert s["ok"] is True
    assert "sleep" in s["why"]
    assert sleep_calls[0][:2] == ["taskkill", "/IM"] and "Moonlight.exe" in sleep_calls[0]
    assert sleep_calls[1][0] == "ssh" and "rtcwake -m no -s 43200" in sleep_calls[1][-1] and "systemctl suspend" in sleep_calls[1][-1]

    monkeypatch.setattr(sol_pad.subprocess, "run", lambda *a, **kw: FakeProc(0, "4\n", ""))
    st_srv = sol_pad.stop_popos_servers()
    assert st_srv["ok"] is True
    assert "stopped 4 Pop!_OS server(s)" in st_srv["why"]

    monkeypatch.setattr(sol_pad.subprocess, "run", lambda *a, **kw: FakeProc(0, "0\n", ""))
    st_none = sol_pad.stop_popos_servers()
    assert st_none["ok"] is True
    assert "no Pop!_OS servers" in st_none["why"]

    pwr_calls = []
    monkeypatch.setattr(sol_pad.subprocess, "run", lambda cmd, **kw: (pwr_calls.append(cmd), FakeProc(0, "", ""))[1])
    po = sol_pad.poweroff_popos()
    assert po["ok"] is True and "powered off" in po["why"]
    assert "systemctl poweroff" in pwr_calls[-1][-1]

    rb = sol_pad.reboot_popos()
    assert rb["ok"] is True and "rebooted" in rb["why"]
    assert "systemctl reboot" in pwr_calls[-1][-1]


def test_popos_details_come_from_the_ignored_config(tmp_path, monkeypatch):
    """IP / MAC stay out of this public repo: data/sol-pad.json (2026-11-23)."""
    import json
    from sol_control_hud.data.collectors import sol_pad
    monkeypatch.setattr("sol_control_hud.paths.DATA_DIR", tmp_path)
    assert sol_pad._popos_config() == {}
    (tmp_path / "sol-pad.json").write_text("[1, 2]")
    assert sol_pad._popos_config() == {}                       # wrong shape: ignored, not a crash
    (tmp_path / "sol-pad.json").write_text(json.dumps({"popos": {"mac": "aa:bb:cc:dd:ee:ff"}}))
    assert sol_pad._popos_config() == {"mac": "aa:bb:cc:dd:ee:ff"}


def test_wake_without_a_mac_says_how_to_fix_it(monkeypatch):
    from sol_control_hud.data.collectors import sol_pad
    import pytest
    with pytest.raises(OSError, match="sol-pad.json"):
        sol_pad.send_wol("")
