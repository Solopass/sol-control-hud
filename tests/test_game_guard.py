"""The game guard's dashboard controls: reading why the AI is off, and the one config line a button may rewrite.

The write is the part that matters. It edits a file the user maintains by hand, outside this repo, so the tests
are mostly about what it refuses and about leaving everything it does not own exactly as it was.
"""
import json

import pytest

from sol_control_hud import game_guard

CONFIG = """{
  "_note": "hand-written, keep the layout",
  "desk": { "marginMB": 2500, "idleUnloadSeconds": 900 },
  "games": {
    "processNames": ["EasyAntiCheat", "vgc"],
    "other3dBusyPercent": 30,
    "ignore3dProcesses": ["msedge", "chrome", "claude"],
    "_ignore3dProcesses": "not games even when they use a lot of the GPU",
    "resumeAfterMinutes": 5
  }
}
"""


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "local-ai.json"
    p.write_text(CONFIG, encoding="utf-8")
    return p


def state_file(tmp_path, mode, reason):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"mode": mode, "reason": reason, "since": "2026-10-08T12:00:00"}), encoding="utf-8")
    return p


# ---- reading the reason

@pytest.mark.parametrize("reason, kind, name, can", [
    ("game: steamwebhelper uses 62% of the GPU", "busy", "steamwebhelper", True),
    ("steamwebhelper uses 62% of the GPU", "busy", "steamwebhelper", True),
    ("game: game process EasyAntiCheat_EOS", "process", "EasyAntiCheat_EOS", False),
    ("game: game cs2", "path", "cs2", False),
    ("game: fullscreen D3D app", "fullscreen", None, False),
    ("game: presentation mode", "fullscreen", None, False),
    ("", None, None, False),
])
def test_what_tripped_the_guard_and_whether_a_name_can_answer_it(reason, kind, name, can):
    """Only the 3D-busy reason is a name to wave away. An anti-cheat service or a Steam install path is a game."""
    t = game_guard.tripped_by(reason)
    assert (t["kind"], t["name"], t["can_ignore"]) == (kind, name, can)


def test_state_offers_the_button_only_while_off_for_a_game(tmp_path, config):
    off = state_file(tmp_path, "off", "game: steamwebhelper uses 62% of the GPU")
    s = game_guard.state(config, off)
    assert s["off_for_a_game"] and s["can_ignore"] and s["name"] == "steamwebhelper"
    desk = state_file(tmp_path, "desk", "game ended")
    back = game_guard.state(config, desk)
    assert back["off_for_a_game"] is False
    assert back["kind"] is None and back["name"] is None    # "game ended" is how it got back, not a game called "ended"


def test_nothing_to_offer_for_something_already_ignored(tmp_path, config):
    s = game_guard.state(config, state_file(tmp_path, "off", "game: claude uses 38% of the GPU"))
    assert s["off_for_a_game"] is True and s["can_ignore"] is False     # claude is in the list already


def test_a_missing_state_file_is_not_an_error(tmp_path, config):
    assert game_guard.state(config, tmp_path / "gone.json") == {"available": False}


# ---- writing the one line

def test_the_name_is_added_and_the_rest_of_the_file_is_untouched(config):
    r = game_guard.ignore("steamwebhelper", config, running={"steamwebhelper"})
    assert r["ok"] and "within 10 s" in r["why"]
    text = config.read_text(encoding="utf-8")
    assert '"ignore3dProcesses": ["msedge", "chrome", "claude", "steamwebhelper"],' in text
    for kept in ('"_note": "hand-written, keep the layout"', '"desk": { "marginMB": 2500, "idleUnloadSeconds": 900 }',
                 '"_ignore3dProcesses": "not games even when they use a lot of the GPU"', '"resumeAfterMinutes": 5'):
        assert kept in text                                             # the layout survives, line for line
    assert json.loads(text)["games"]["other3dBusyPercent"] == 30        # thresholds untouched


def test_adding_the_same_name_twice_changes_nothing(config):
    game_guard.ignore("steamwebhelper", config, running={"steamwebhelper"})
    before = config.read_text(encoding="utf-8")
    r = game_guard.ignore("steamwebhelper", config, running={"steamwebhelper"})
    assert r["ok"] and "already ignored" in r["why"]
    assert config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("name", ["", "   ", "has space", "../../etc/passwd", 'quote"', "a" * 80, "semi;colon"])
def test_a_name_that_is_not_a_process_name_is_refused(config, name):
    before = config.read_text(encoding="utf-8")
    r = game_guard.ignore(name, config, running={name.strip().lower()})
    assert r["ok"] is False and "not a process name" in r["why"]
    assert config.read_text(encoding="utf-8") == before


def test_a_name_that_is_not_running_is_refused(config):
    """The page sends a name; a name from anywhere else has no business in the machine's config."""
    before = config.read_text(encoding="utf-8")
    r = game_guard.ignore("cs2", config, running={"explorer"})
    assert r["ok"] is False and "no process called cs2 is running" in r["why"]
    assert config.read_text(encoding="utf-8") == before


def test_exe_suffix_is_accepted_and_stored_without_it(config):
    """Get-Process -Name in the watcher wants bare names; the page may well send steamwebhelper.exe."""
    assert game_guard.ignore("steamwebhelper.exe", config, running={"steamwebhelper"})["ok"]
    assert json.loads(config.read_text(encoding="utf-8"))["games"]["ignore3dProcesses"][-1] == "steamwebhelper"


def test_a_config_without_the_line_is_left_alone(tmp_path):
    p = tmp_path / "local-ai.json"
    p.write_text('{"games": {"other3dBusyPercent": 30}}', encoding="utf-8")
    r = game_guard.ignore("steamwebhelper", p, running={"steamwebhelper"})
    assert r["ok"] is False and "could not find ignore3dProcesses" in r["why"]
    assert p.read_text(encoding="utf-8") == '{"games": {"other3dBusyPercent": 30}}'


def test_a_byte_order_mark_survives(tmp_path):
    p = tmp_path / "local-ai.json"
    p.write_bytes(b"\xef\xbb\xbf" + CONFIG.encode("utf-8"))
    assert game_guard.ignore("steamwebhelper", p, running={"steamwebhelper"})["ok"]
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(p.read_text(encoding="utf-8-sig"))["games"]["ignore3dProcesses"][-1] == "steamwebhelper"


def test_ignored_reads_the_live_list(config):
    assert game_guard.ignored(config) == ["msedge", "chrome", "claude"]
    assert game_guard.ignored(config.with_name("nope.json")) == []
