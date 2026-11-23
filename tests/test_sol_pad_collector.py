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
    res = sol_pad.open_popos_rdp()
    assert res["ok"] is True
    assert "popos.lan" in res["why"]
    assert calls[0][0] == ["mstsc.exe", "/v:popos.lan"]

    def fail_popen(cmd, **kwargs):
        raise OSError("not found")

    monkeypatch.setattr(sol_pad.subprocess, "Popen", fail_popen)
    err = sol_pad.open_popos_rdp()
    assert err["ok"] is False
    assert "not found" in err["why"]
