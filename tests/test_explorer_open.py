"""Opening a folder from the dashboard brings its Explorer window to the front."""
from pathlib import Path

from sol_control_hud import explorer_open as eo


def test_pick_prefers_the_new_window_then_one_titled_with_the_folder():
    folder = Path(r"D:\Workspace\cognispan")
    wins = [(5, "Downloads"), (7, "cognispan"), (9, "Music")]
    assert eo.pick(wins, {5, 7}, folder) == (9, True)                 # appeared after the open
    assert eo.pick(wins, {5, 7, 9}, folder) == (7, False)             # Explorer reused its window
    assert eo.pick([(3, r"D:\Workspace\cognispan")], {3}, folder) == (3, False)   # "full path in title" setting
    assert eo.pick([(3, "Music")], {3}, folder) == (None, False)


def test_waits_for_the_new_window_then_fronts_it():
    t = {"now": 0.0}
    seen = [[(1, "Downloads")], [(1, "Downloads")], [(1, "Downloads"), (2, "cognispan")]]
    fronted = []
    how = eo.focus_when_ready(Path(r"D:\Workspace\cognispan"), {1}, wait_s=4, clock=lambda: t["now"],
                              sleep=lambda s: t.__setitem__("now", t["now"] + s),
                              windows=lambda: seen.pop(0) if len(seen) > 1 else seen[0],
                              front=lambda h: fronted.append(h) or "join-input")
    assert how == "join-input" and fronted == [2] and t["now"] < 1


def test_no_window_means_false_not_a_crash():
    t = {"now": 0.0}
    how = eo.focus_when_ready(Path(r"D:\x"), set(), wait_s=1, clock=lambda: t["now"],
                              sleep=lambda s: t.__setitem__("now", t["now"] + s), windows=lambda: [],
                              front=lambda h: True)
    assert how is False


def test_launcher_opens_folders_through_it(monkeypatch, tmp_path):
    from sol_control_hud import launcher
    opened = []
    monkeypatch.setattr(eo, "open_folder", lambda p: opened.append(Path(p)))
    monkeypatch.setattr(launcher.os, "startfile", lambda p: opened.append(("startfile", p)), raising=False)
    launcher._open_path(tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("x")
    launcher._open_path(f)
    assert opened == [tmp_path, ("startfile", str(f))]       # a folder: Explorer to the front; a file: its default app


def test_a_reused_window_comes_forward_after_a_short_grace_not_the_full_wait():
    t = {"now": 0.0}
    fronted = []
    how = eo.focus_when_ready(Path(r"D:\Workspace\cognispan"), {7}, wait_s=4, clock=lambda: t["now"],
                              sleep=lambda s: t.__setitem__("now", t["now"] + s), windows=lambda: [(7, "cognispan")],
                              front=lambda h: fronted.append(h) or "join-input")
    assert how == "join-input" and fronted == [7] and t["now"] < 1.5
