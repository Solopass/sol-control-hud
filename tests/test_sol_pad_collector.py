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


def test_open_popos_rdp(monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))

    monkeypatch.setattr(sol_pad.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sol_pad, "_tcp_up", lambda host, port=3389, timeout=0.35: True)
    res = sol_pad.open_popos_rdp()
    assert res["ok"] is True
    assert "opened Remote Desktop" in res["why"]
    assert calls[0][0] == ["mstsc.exe", "/v:popos.lan"]

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
