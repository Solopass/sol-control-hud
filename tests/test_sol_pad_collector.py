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
