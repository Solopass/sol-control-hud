"""Opening a note in Obsidian brings Obsidian to the front, once it shows that note."""
from pathlib import Path

from sol_control_hud import obsidian_open as oo


def test_a_vault_file_becomes_an_obsidian_link(tmp_path):
    vault = tmp_path / "Polymatica Vault"
    (vault / "2026").mkdir(parents=True)
    note = vault / "2026" / "2026-10-08.md"
    note.write_text("x")
    uri, name, stem = oo.uri_for_path(note, {"Polymatica Vault": vault, "Other": tmp_path / "elsewhere"})
    assert uri == "obsidian://open?vault=Polymatica%20Vault&file=2026/2026-10-08"
    assert (name, stem) == ("Polymatica Vault", "2026-10-08")
    assert oo.uri_for_path(tmp_path / "loose.md", {"Polymatica Vault": vault}) is None   # outside every vault


WINDOWS = [(1, "Some other note - OBVLT - Obsidian 1.14.4"),
           (2, "2026-10-08 - Polymatica Vault - Obsidian 1.14.4"),
           (3, "Plans - OBVLT - Obsidian 1.14.4")]


def test_pick_the_window_that_shows_the_note_then_its_vault():
    assert oo.pick(WINDOWS, "2026-10-08", "Polymatica Vault") == (2, True)
    assert oo.pick(WINDOWS, "New note", "OBVLT") == (1, False)          # not there yet: the vault's front window
    assert oo.pick(WINDOWS, "x", "Unknown vault") == (1, False)         # any Obsidian window beats none
    assert oo.pick([], "x", "OBVLT") == (None, False)


def test_waits_for_the_note_before_bringing_it_forward():
    t = {"now": 0.0}
    seen = [[(1, "Plans - OBVLT - Obsidian")], [(1, "Plans - OBVLT - Obsidian")], [(1, "SOL plan - OBVLT - Obsidian")]]
    fronted = []
    ok = oo.focus_when_ready("SOL plan", "OBVLT", wait_s=5, clock=lambda: t["now"],
                             sleep=lambda s: t.__setitem__("now", t["now"] + s),
                             windows=lambda: seen.pop(0) if len(seen) > 1 else seen[0],
                             front=lambda h: fronted.append(h) or True)
    assert ok and fronted == [1] and t["now"] < 1          # fronted as soon as the title showed the note


def test_gives_up_waiting_but_still_brings_obsidian_forward():
    t = {"now": 0.0}
    fronted = []
    oo.focus_when_ready("Never shows", "OBVLT", wait_s=1, clock=lambda: t["now"],
                        sleep=lambda s: t.__setitem__("now", t["now"] + s),
                        windows=lambda: [(7, "Plans - OBVLT - Obsidian")], front=lambda h: fronted.append(h) or True)
    assert fronted == [7]
    assert oo.focus_when_ready("x", "y", wait_s=0.3, clock=lambda: t["now"], sleep=lambda s: t.__setitem__("now", t["now"] + s),
                               windows=lambda: [], front=lambda h: True) is False   # Obsidian never appeared
