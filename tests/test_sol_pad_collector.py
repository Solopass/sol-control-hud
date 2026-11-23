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
    assert calls[0][0] == [str(fake_moonlight), "--display-mode", "windowed", "stream", "popos.lan", "Desktop"]
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


def test_popos_status_wake_and_sleep(monkeypatch):
    sol_pad._popos_cache["at"] = 0.0
    sol_pad._popos_cache["data"] = {}
    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=22, timeout=0.25: False)
    st_off = sol_pad.get_popos_status()
    assert st_off == {"online": False, "state": "sleeping"}

    class FakeProc:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

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

    wols = []
    monkeypatch.setattr(sol_pad, "send_wol", lambda mac=sol_pad.POPOS_MAC: wols.append(mac))
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
    assert sleep_calls[1][0] == "ssh" and "systemctl suspend" in sleep_calls[1][-1]
