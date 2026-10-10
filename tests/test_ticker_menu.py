"""Every item in the ticker's right-click menu runs without an error.

Why: "Switch AI to Away" raised NameError on every click from the app's first commit (ticker.py never imported
subprocess) and an `except Exception: pass` hid it until 10-08. No test ever clicked it. This builds the real menu
and invokes every enabled item, with everything outside the app faked: processes, files, URLs, the AI router, the
Windows startup shortcut, sounds, Exit. An error inside a click - raised, or written down by swallow.note - fails it.
"""
import types

import pytest


@pytest.fixture
def ticker_app(monkeypatch, tmp_path):
    import os
    import subprocess
    import tkinter as tk
    import webbrowser
    from tkinter import messagebox
    from sol_control_hud.views import ticker, ticker_base, ticker_layout, ticker_render, ticker_widgets, ticker_win

    effects = {"run": [], "popen": [], "startfile": [], "web": [], "hub": [], "swallowed": [], "startup": [], "obsidian": []}
    monkeypatch.setattr(ticker, "SETTINGS_FILE", tmp_path / "ticker-settings.json")
    monkeypatch.setattr(ticker, "TRIGGER_FILE", tmp_path / "ticker.trigger")
    monkeypatch.setattr(ticker, "ACK_FILE", tmp_path / "stability-ack.json")
    monkeypatch.setattr(ticker, "LLM_DIR", tmp_path / "llm")                     # never the real Away stop flag
    (tmp_path / "llm").mkdir()
    sol_llm = tmp_path / "sol-llm.ps1"
    sol_llm.write_text("# fake")
    monkeypatch.setattr(ticker, "SOL_LLM", sol_llm)
    vault = tmp_path / "Polymatica Vault"                                          # never the real daily note
    monkeypatch.setattr(ticker, "DAILY_VAULT", vault)
    chains = tmp_path / "Chains"
    chains.mkdir()
    (chains / "Weekly digest.md").write_text("---\ntype: chain\nstatus: done\n---\n## Step\ncheck: x\nhello\n", encoding="utf-8")
    monkeypatch.setattr(ticker, "CHAINS_DIR", chains)
    monkeypatch.setattr(ticker, "router_models", lambda: [("sol-fast", "loaded", "sol-fast"), ("sol-smart", "unloaded", "sol-smart")])
    monkeypatch.setattr(ticker, "is_startup_enabled", lambda: False)
    monkeypatch.setattr(ticker, "set_startup", lambda on: effects["startup"].append(on))
    def fake_run(*a, **k):
        effects["run"].append(a[0] if a else k)
        return subprocess.CompletedProcess(a[0] if a else [], 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: effects["popen"].append(a[0] if a else k))
    monkeypatch.setattr(os, "startfile", lambda *a, **k: effects["startfile"].append(a[0]), raising=False)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: effects["web"].append(a[0]))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(ticker_render, "play_alert_sound", lambda *a, **k: None)
    from sol_control_hud import obsidian_open
    monkeypatch.setattr(obsidian_open, "open_uri", lambda uri, note, vault_: effects["obsidian"].append(uri))
    import httpx

    class NoRouter:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, **k): effects["run"].append(("router", url)); return types.SimpleNamespace(status_code=200, json=lambda: {})
        def get(self, url, **k): return types.SimpleNamespace(status_code=200, json=lambda: {"data": []})
    monkeypatch.setattr(httpx, "Client", NoRouter)
    # background work runs right away, so its errors happen inside the click
    sync = types.SimpleNamespace(Thread=lambda target=None, daemon=None, name=None, args=(), kwargs=None:
                                 types.SimpleNamespace(start=lambda: target(*args, **(kwargs or {}))))
    monkeypatch.setattr(ticker, "threading", sync)
    for mod in (ticker, ticker_base, ticker_layout, ticker_render, ticker_widgets, ticker_win):
        if hasattr(mod, "_swallowed"):
            monkeypatch.setattr(mod, "_swallowed", lambda where: effects["swallowed"].append(where))

    class Hub:
        def __getattr__(self, name):
            return lambda *a, **k: effects["hub"].append((name, a))

    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda exc, val, tb: errors.append(f"{exc.__name__}: {val}")
    app = ticker.TickerApp(root, hub=Hub())
    app.quit = lambda why="": effects["hub"].append(("quit", why))               # Exit must not end the test run
    menus = []
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, x, y, entry="": menus.append(self))
    yield app, menus, errors, effects
    root.destroy()


def _walk(menu):
    """Every enabled command in a menu and its submenus: (menu, index, label)."""
    out = []
    last = menu.index("end")
    for i in range(0, (last if last is not None else -1) + 1):
        kind = menu.type(i)
        if kind == "cascade":
            out += _walk(menu.nametowidget(menu.entrycget(i, "menu")))
        elif kind == "command" and menu.entrycget(i, "state") != "disabled":
            out.append((menu, i, menu.entrycget(i, "label")))
    return out


def _click_everything(app, menus, errors, effects):
    app._show_context_menu(types.SimpleNamespace(x_root=10, y_root=10))
    items = _walk(menus[-1])
    clicked = []
    for menu, i, label in items:
        before = (len(errors), len(effects["swallowed"]))
        menu.invoke(i)
        app.root.update()
        if (len(errors), len(effects["swallowed"])) != before:
            clicked.append(f"{label!r}: {errors[before[0]:] + effects['swallowed'][before[1]:]}")
    return items, clicked


def test_every_menu_item_runs_at_the_desk(ticker_app):
    app, menus, errors, effects = ticker_app
    app.latest_snap.ai_mode = "desk"
    items, failed = _click_everything(app, menus, errors, effects)
    assert len(items) > 40                                    # the whole menu, not an empty one
    assert failed == [], "menu items that failed:\n" + "\n".join(failed)
    assert any(str(cmd[-1]) == "away" for cmd in effects["run"] if isinstance(cmd, list))   # Away really ran the script
    assert effects["obsidian"], "Open Daily Note went to Obsidian"
    assert ("do_action", ("login_start", "on")) in effects["hub"]                   # with the hub: one switch for it
    assert ("quit", "menu Exit") in effects["hub"] and effects["startup"] == []


def test_every_menu_item_runs_during_away_with_a_crash_showing(ticker_app):
    app, menus, errors, effects = ticker_app
    app.latest_snap.ai_mode = "away"
    app.latest_snap.unexpected_reboots = 1                    # adds the crash items
    items, failed = _click_everything(app, menus, errors, effects)
    labels = [label for _, _, label in items]
    assert any("Stop AI work" in label for label in labels) and any("Clear Crash" in label for label in labels)
    assert failed == [], "menu items that failed:\n" + "\n".join(failed)
    from sol_control_hud.views import ticker
    assert (ticker.LLM_DIR / "away-stop.flag").exists()                            # Stop AI work: in the test folder only
